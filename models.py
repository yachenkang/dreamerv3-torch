import copy
import torch
from torch import nn

import networks
import tools

import numpy as np

to_np = lambda x: x.detach().cpu().numpy()


class RewardEMA:
    """running mean and std"""

    def __init__(self, device, alpha=1e-2):
        self.device = device
        self.alpha = alpha
        self.range = torch.tensor([0.05, 0.95]).to(device)

    def __call__(self, x, ema_vals):
        flat_x = torch.flatten(x.detach())
        x_quantile = torch.quantile(input=flat_x, q=self.range)
        # this should be in-place operation
        ema_vals[:] = self.alpha * x_quantile + (1 - self.alpha) * ema_vals
        scale = torch.clip(ema_vals[1] - ema_vals[0], min=1.0)
        offset = ema_vals[0]
        return offset.detach(), scale.detach()


class WorldModel(nn.Module):
    def __init__(self, obs_space, act_space, step, config):
        super(WorldModel, self).__init__()
        self._step = step
        self._use_amp = True if config.precision == 16 else False
        self._config = config
        shapes = {k: tuple(v.shape) for k, v in obs_space.spaces.items()}
        self.encoder = networks.MultiEncoder(shapes, **config.encoder)
        self.embed_size = self.encoder.outdim
        self.dynamics = networks.RSSM(
            config.dyn_stoch,
            config.dyn_deter,
            config.dyn_hidden,
            config.dyn_rec_depth,
            config.dyn_discrete,
            config.act,
            config.norm,
            config.dyn_mean_act,
            config.dyn_std_act,
            config.dyn_min_std,
            config.unimix_ratio,
            config.initial,
            config.num_actions,
            self.embed_size,
            config.device,
        )
        self.heads = nn.ModuleDict()
        if config.dyn_discrete:
            feat_size = config.dyn_stoch * config.dyn_discrete + config.dyn_deter
        else:
            feat_size = config.dyn_stoch + config.dyn_deter
        self.heads["decoder"] = networks.MultiDecoder(
            feat_size, shapes, **config.decoder
        )
        self.heads["reward"] = networks.MLP(
            feat_size,
            (255,) if config.reward_head["dist"] == "symlog_disc" else (),
            config.reward_head["layers"],
            config.units,
            config.act,
            config.norm,
            dist=config.reward_head["dist"],
            outscale=config.reward_head["outscale"],
            device=config.device,
            name="Reward",
        )
        self.heads["cont"] = networks.MLP(
            feat_size,
            (),
            config.cont_head["layers"],
            config.units,
            config.act,
            config.norm,
            dist="binary",
            outscale=config.cont_head["outscale"],
            device=config.device,
            name="Cont",
        )
        for name in config.grad_heads:
            assert name in self.heads, name
        self._model_opt = tools.Optimizer(
            "model",
            self.parameters(),
            config.model_lr,
            config.opt_eps,
            config.grad_clip,
            config.weight_decay,
            opt=config.opt,
            use_amp=self._use_amp,
        )
        print(
            f"Optimizer model_opt has {sum(param.numel() for param in self.parameters())} variables."
        )
        # other losses are scaled by 1.0.
        self._scales = dict(
            reward=config.reward_head["loss_scale"],
            cont=config.cont_head["loss_scale"],
        )

    def _train(self, data):
        # action (batch_size, batch_length, act_dim)
        # image (batch_size, batch_length, h, w, ch)
        # reward (batch_size, batch_length)
        # discount (batch_size, batch_length)
        data = self.preprocess(data)

        with tools.RequiresGrad(self):
            with torch.cuda.amp.autocast(self._use_amp):
                embed = self.encoder(data)
                post, prior = self.dynamics.observe(
                    embed, data["action"], data["is_first"]
                )
                kl_free = self._config.kl_free
                dyn_scale = self._config.dyn_scale
                rep_scale = self._config.rep_scale
                kl_loss, kl_value, dyn_loss, rep_loss = self.dynamics.kl_loss(
                    post, prior, kl_free, dyn_scale, rep_scale
                )
                assert kl_loss.shape == embed.shape[:2], kl_loss.shape
                preds = {}
                for name, head in self.heads.items():
                    grad_head = name in self._config.grad_heads
                    feat = self.dynamics.get_feat(post)
                    feat = feat if grad_head else feat.detach()
                    pred = head(feat)
                    if type(pred) is dict:
                        preds.update(pred)
                    else:
                        preds[name] = pred
                losses = {}
                for name, pred in preds.items():
                    loss = -pred.log_prob(data[name])
                    assert loss.shape == embed.shape[:2], (name, loss.shape)
                    losses[name] = loss
                scaled = {
                    key: value * self._scales.get(key, 1.0)
                    for key, value in losses.items()
                }
                model_loss = sum(scaled.values()) + kl_loss
            metrics = self._model_opt(torch.mean(model_loss), self.parameters())

        metrics.update({f"{name}_loss": to_np(loss) for name, loss in losses.items()})
        metrics["kl_free"] = kl_free
        metrics["dyn_scale"] = dyn_scale
        metrics["rep_scale"] = rep_scale
        metrics["dyn_loss"] = to_np(dyn_loss)
        metrics["rep_loss"] = to_np(rep_loss)
        metrics["kl"] = to_np(torch.mean(kl_value))
        with torch.cuda.amp.autocast(self._use_amp):
            metrics["prior_ent"] = to_np(
                torch.mean(self.dynamics.get_dist(prior).entropy())
            )
            metrics["post_ent"] = to_np(
                torch.mean(self.dynamics.get_dist(post).entropy())
            )
            context = dict(
                embed=embed,
                feat=self.dynamics.get_feat(post),
                kl=kl_value,
                postent=self.dynamics.get_dist(post).entropy(),
            )
        post = {k: v.detach() for k, v in post.items()}
        return post, context, metrics

    # this function is called during both rollout and training
    def preprocess(self, obs):
        obs = obs.copy()
        obs["image"] = torch.Tensor(obs["image"].copy()) / 255.0
        if "discount" in obs:
            obs["discount"] *= self._config.discount
            # (batch_size, batch_length) -> (batch_size, batch_length, 1)
            obs["discount"] = torch.Tensor(obs["discount"]).unsqueeze(-1)
        # 'is_first' is necesarry to initialize hidden state at training
        assert "is_first" in obs
        # 'is_terminal' is necesarry to train cont_head
        assert "is_terminal" in obs
        obs["cont"] = torch.Tensor(1.0 - obs["is_terminal"]).unsqueeze(-1)
        obs = {k: torch.Tensor(v).to(self._config.device) for k, v in obs.items()}
        return obs

    def video_pred(self, data):
        data = self.preprocess(data)
        embed = self.encoder(data)

        states, _ = self.dynamics.observe(
            embed[:6, :5], data["action"][:6, :5], data["is_first"][:6, :5]
        )
        recon = self.heads["decoder"](self.dynamics.get_feat(states))["image"].mode()[
            :6
        ]
        reward_post = self.heads["reward"](self.dynamics.get_feat(states)).mode()[:6]
        init = {k: v[:, -1] for k, v in states.items()}
        prior = self.dynamics.imagine_with_action(data["action"][:6, 5:], init)
        openl = self.heads["decoder"](self.dynamics.get_feat(prior))["image"].mode()
        reward_prior = self.heads["reward"](self.dynamics.get_feat(prior)).mode()
        # observed image is given until 5 steps
        model = torch.cat([recon[:, :5], openl], 1)
        truth = data["image"][:6]
        model = model
        error = (model - truth + 1.0) / 2.0

        return torch.cat([truth, model, error], 2)


class ImagBehavior(nn.Module):
    def __init__(self, config, world_model):
        super(ImagBehavior, self).__init__()
        self._use_amp = True if config.precision == 16 else False
        self._config = config
        self._world_model = world_model
        if config.dyn_discrete:
            feat_size = config.dyn_stoch * config.dyn_discrete + config.dyn_deter
        else:
            feat_size = config.dyn_stoch + config.dyn_deter
        self.actor = networks.MLP(
            feat_size,
            (config.num_actions,),
            config.actor["layers"],
            config.units,
            config.act,
            config.norm,
            config.actor["dist"],
            config.actor["std"],
            config.actor["min_std"],
            config.actor["max_std"],
            absmax=1.0,
            temp=config.actor["temp"],
            unimix_ratio=config.actor["unimix_ratio"],
            outscale=config.actor["outscale"],
            name="Actor",
        )
        self.value = networks.MLP(
            feat_size,
            (255,) if config.critic["dist"] == "symlog_disc" else (),
            config.critic["layers"],
            config.units,
            config.act,
            config.norm,
            config.critic["dist"],
            outscale=config.critic["outscale"],
            device=config.device,
            name="Value",
        )
        if config.critic["slow_target"]:
            self._slow_value = copy.deepcopy(self.value)
            self._updates = 0
        kw = dict(wd=config.weight_decay, opt=config.opt, use_amp=self._use_amp)
        self._actor_opt = tools.Optimizer(
            "actor",
            self.actor.parameters(),
            config.actor["lr"],
            config.actor["eps"],
            config.actor["grad_clip"],
            **kw,
        )
        print(
            f"Optimizer actor_opt has {sum(param.numel() for param in self.actor.parameters())} variables."
        )
        self._value_opt = tools.Optimizer(
            "value",
            self.value.parameters(),
            config.critic["lr"],
            config.critic["eps"],
            config.critic["grad_clip"],
            **kw,
        )
        print(
            f"Optimizer value_opt has {sum(param.numel() for param in self.value.parameters())} variables."
        )
        if self._config.reward_EMA:
            # register ema_vals to nn.Module for enabling torch.save and torch.load
            self.register_buffer("ema_vals", torch.zeros((2,)).to(self._config.device))
            self.reward_ema = RewardEMA(device=self._config.device)

    def _train(
        self,
        start,
        objective,
        mf_policy
    ):
        self._update_slow_target()
        metrics = {}

        with tools.RequiresGrad(self.actor):
            with torch.cuda.amp.autocast(self._use_amp):
                imag_feat, imag_state, imag_action = self._imagine(
                    start, self.actor, self._config.imag_horizon
                )
                reward = objective(imag_feat, imag_state, imag_action)
                actor_ent = self.actor(imag_feat).entropy()
                state_ent = self._world_model.dynamics.get_dist(imag_state).entropy()
                # this target is not scaled by ema or sym_log.
                target, weights, base = self._compute_target(
                    imag_feat, imag_state, reward
                )
                actor_loss, mets = self._compute_actor_loss(
                    imag_feat,
                    imag_action,
                    target,
                    weights,
                    base,
                )
                actor_loss -= self._config.actor["entropy"] * actor_ent[:-1, ..., None]

                if self._config.mf_reg:
                    inp = imag_feat[:-1].detach()
                    actor_loss += torch.distributions.kl.kl_divergence(self.actor(inp)._dist, mf_policy(inp)._dist)[:,:,None] * self._config.mf_reg_scale

                actor_loss = torch.mean(actor_loss)
                metrics.update(mets)
                value_input = imag_feat

        with tools.RequiresGrad(self.value):
            with torch.cuda.amp.autocast(self._use_amp):
                value = self.value(value_input[:-1].detach())
                target = torch.stack(target, dim=1)
                # (time, batch, 1), (time, batch, 1) -> (time, batch)
                value_loss = -value.log_prob(target.detach())
                slow_target = self._slow_value(value_input[:-1].detach())
                if self._config.critic["slow_target"]:
                    value_loss -= value.log_prob(slow_target.mode().detach())
                # (time, batch, 1), (time, batch, 1) -> (1,)
                value_loss = torch.mean(weights[:-1] * value_loss[:, :, None])

        metrics.update(tools.tensorstats(value.mode(), "value"))
        metrics.update(tools.tensorstats(target, "target"))
        metrics.update(tools.tensorstats(reward, "imag_reward"))
        if self._config.actor["dist"] in ["onehot"]:
            metrics.update(
                tools.tensorstats(
                    torch.argmax(imag_action, dim=-1).float(), "imag_action"
                )
            )
        else:
            metrics.update(tools.tensorstats(imag_action, "imag_action"))
        metrics["actor_entropy"] = to_np(torch.mean(actor_ent))
        with tools.RequiresGrad(self):
            metrics.update(self._actor_opt(actor_loss, self.actor.parameters()))
            metrics.update(self._value_opt(value_loss, self.value.parameters()))
        return imag_feat, imag_state, imag_action, weights, metrics

    def _imagine(self, start, policy, horizon):
        dynamics = self._world_model.dynamics
        flatten = lambda x: x.reshape([-1] + list(x.shape[2:]))
        start = {k: flatten(v) for k, v in start.items()}

        def step(prev, _):
            state, _, _ = prev
            feat = dynamics.get_feat(state)
            inp = feat.detach()
            action = policy(inp).sample()
            succ = dynamics.img_step(state, action)
            return succ, feat, action

        succ, feats, actions = tools.static_scan(
            step, [torch.arange(horizon)], (start, None, None)
        )
        states = {k: torch.cat([start[k][None], v[:-1]], 0) for k, v in succ.items()}

        return feats, states, actions

    def _compute_target(self, imag_feat, imag_state, reward):
        if "cont" in self._world_model.heads:
            inp = self._world_model.dynamics.get_feat(imag_state)
            discount = self._config.discount * self._world_model.heads["cont"](inp).mean
        else:
            discount = self._config.discount * torch.ones_like(reward)
        value = self.value(imag_feat).mode()
        target = tools.lambda_return(
            reward[1:],
            value[:-1],
            discount[1:],
            bootstrap=value[-1],
            lambda_=self._config.discount_lambda,
            axis=0,
        )
        weights = torch.cumprod(
            torch.cat([torch.ones_like(discount[:1]), discount[:-1]], 0), 0
        ).detach()
        return target, weights, value[:-1]

    def _compute_actor_loss(
        self,
        imag_feat,
        imag_action,
        target,
        weights,
        base,
    ):
        metrics = {}
        inp = imag_feat.detach()
        policy = self.actor(inp)
        # Q-val for actor is not transformed using symlog
        target = torch.stack(target, dim=1)
        if self._config.reward_EMA:
            offset, scale = self.reward_ema(target, self.ema_vals)
            normed_target = (target - offset) / scale
            normed_base = (base - offset) / scale
            adv = normed_target - normed_base
            metrics.update(tools.tensorstats(normed_target, "normed_target"))
            metrics["EMA_005"] = to_np(self.ema_vals[0])
            metrics["EMA_095"] = to_np(self.ema_vals[1])

        if self._config.imag_gradient == "dynamics":
            actor_target = adv
        elif self._config.imag_gradient == "reinforce":
            actor_target = (
                policy.log_prob(imag_action)[:-1][:, :, None]
                * (target - self.value(imag_feat[:-1]).mode()).detach()
            )
        elif self._config.imag_gradient == "both":
            actor_target = (
                policy.log_prob(imag_action)[:-1][:, :, None]
                * (target - self.value(imag_feat[:-1]).mode()).detach()
            )
            mix = self._config.imag_gradient_mix
            actor_target = mix * target + (1 - mix) * actor_target
            metrics["imag_gradient_mix"] = mix
        else:
            raise NotImplementedError(self._config.imag_gradient)
        actor_loss = -weights[:-1] * actor_target
        return actor_loss, metrics

    def _update_slow_target(self):
        if self._config.critic["slow_target"]:
            if self._updates % self._config.critic["slow_target_update"] == 0:
                mix = self._config.critic["slow_target_fraction"]
                for s, d in zip(self.value.parameters(), self._slow_value.parameters()):
                    d.data = mix * s.data + (1 - mix) * d.data
            self._updates += 1

class Behavior(nn.Module):
    def __init__(self, config, world_model):
        super(Behavior, self).__init__()
        self._use_amp = True if config.precision == 16 else False
        self._config = config
        self._world_model = world_model
        # self._dataset = dataset
        if config.dyn_discrete:
            feat_size = config.dyn_stoch * config.dyn_discrete + config.dyn_deter
        else:
            feat_size = config.dyn_stoch + config.dyn_deter
        # # use decoder only
        # feat_size = self._world_model.embed_size
        self.actor = networks.MLP(
            feat_size,
            (config.num_actions,),
            config.actor["layers"],
            config.units,
            config.act,
            config.norm,
            config.actor["dist"],
            config.actor["std"],
            config.actor["min_std"],
            config.actor["max_std"],
            absmax=1.0,
            temp=config.actor["temp"],
            unimix_ratio=config.actor["unimix_ratio"],
            outscale=config.actor["outscale"],
            name="Actor",
        )
        # # use decoder only
        # feat_size += config.num_actions
        self.value_1 = networks.MLP(
            feat_size,
            (255,) if config.critic["dist"] == "symlog_disc" else (),
            config.critic["layers"],
            config.units,
            config.act,
            config.norm,
            config.critic["dist"],
            outscale=config.critic["outscale"],
            device=config.device,
            name="Value",
        )
        self.value_2 = networks.MLP(
            feat_size,
            (255,) if config.critic["dist"] == "symlog_disc" else (),
            config.critic["layers"],
            config.units,
            config.act,
            config.norm,
            config.critic["dist"],
            outscale=config.critic["outscale"],
            device=config.device,
            name="Value",
        )
        if config.critic["slow_target"]:
            self._slow_value_1 = copy.deepcopy(self.value_1)
            self._slow_value_2 = copy.deepcopy(self.value_2)
            self._updates = 0
        kw = dict(wd=config.weight_decay, opt=config.opt, use_amp=self._use_amp)
        self._actor_opt = tools.Optimizer(
            "mf_actor",
            self.actor.parameters(),
            config.actor["lr"],
            config.actor["eps"],
            config.actor["grad_clip"],
            **kw,
        )
        print(
            f"Optimizer actor_opt has {sum(param.numel() for param in self.actor.parameters())} variables."
        )
        self._value_opt_1 = tools.Optimizer(
            "mf_value_1",
            self.value_1.parameters(),
            config.critic["lr"],
            config.critic["eps"],
            config.critic["grad_clip"],
            **kw,
        )
        print(
            f"Optimizer value_opt has {sum(param.numel() for param in self.value_1.parameters())} variables."
        )
        self._value_opt_2 = tools.Optimizer(
            "mf_value_2",
            self.value_2.parameters(),
            config.critic["lr"],
            config.critic["eps"],
            config.critic["grad_clip"],
            **kw,
        )
        print(
            f"Optimizer value_opt has {sum(param.numel() for param in self.value_2.parameters())} variables."
        )
        self.total_it = 0
        if self._config.reward_EMA:
            # register ema_vals to nn.Module for enabling torch.save and torch.load
            self.register_buffer("ema_vals", torch.zeros((2,)).to(self._config.device))
            self.reward_ema = RewardEMA(device=self._config.device)

    def _train(
        self,
        start,
        data,
    ):
        self._update_slow_target()
        metrics = {}
        mf = "mf"
        self.total_it += 1

        with tools.RequiresGrad(self.actor):
            with torch.cuda.amp.autocast(self._use_amp):

                # reuse feat_extractor

                feat, next_feat, state, action, reward, discount = self._data_sample(start, data)
                actor_ent = self.actor(feat).entropy()
                # this target is not scaled by ema or sym_log.
                target, weights, base = self._compute_target(
                    next_feat, state, reward, discount
                )
                if self.total_it % self._config.mf_policy_freq == 0:
                    actor_loss, mets = self._compute_actor_loss(
                        feat,
                        action,
                        target,
                        weights,
                        base,
                        state,
                    )
                    actor_loss -= self._config.actor["entropy"] * actor_ent[:-1, ..., None]
                    actor_loss = torch.mean(actor_loss)
                    metrics.update(mets)
                value_input = feat

                # # resue decoder only

                # embed, action, next_action, reward, discount = self._data_sample(start, data)
                # actor_ent = self.actor(embed).entropy()
                # # this target is not scaled by ema or sym_log.
                # target, weights, base = self._compute_target(
                #     embed, next_action, reward, discount
                # )
                # if self.total_it % self._config.mf_policy_freq == 0:
                #     actor_loss, mets = self._compute_actor_loss(
                #         embed,
                #         action,
                #         target,
                #         weights,
                #         base,
                #     )
                #     actor_loss -= self._config.actor["entropy"] * actor_ent[:-1, ..., None]
                #     actor_loss = torch.mean(actor_loss)
                #     metrics.update(mets)
                # value_input = torch.cat([embed, action], dim=-1)


        with tools.RequiresGrad(self.value_1):
            with tools.RequiresGrad(self.value_2):
                with torch.cuda.amp.autocast(self._use_amp):
                    value_1 = self.value_1(value_input[:-1].detach())
                    value_2 = self.value_2(value_input[:-1].detach())
                    value = torch.min(value_1.mode(), value_2.mode())
                    target = torch.stack(target, dim=1)
                    # (time, batch, 1), (time, batch, 1) -> (time, batch)
                    value_loss = -value_1.log_prob(target.detach()) -value_2.log_prob(target.detach())
                    slow_target_1 = self._slow_value_1(value_input[:-1].detach())
                    slow_target_2 = self._slow_value_2(value_input[:-1].detach())
                    if self._config.critic["slow_target"]:
                        value_loss -= value_1.log_prob(slow_target_1.mode().detach()) + value_2.log_prob(slow_target_2.mode().detach())
                    # (time, batch, 1), (time, batch, 1) -> (1,)
                    value_loss = torch.mean(weights[:-1] * value_loss[:, :, None])

        metrics.update(tools.tensorstats(value, "mf_value"))
        metrics.update(tools.tensorstats(target, "mf_target"))
        metrics.update(tools.tensorstats(reward, "mf_reward"))
        if self._config.actor["dist"] in ["onehot"]:
            metrics.update(
                tools.tensorstats(
                    torch.argmax(action, dim=-1).float(), "mf_action"
                )
            )
        else:
            metrics.update(tools.tensorstats(action, "mf_action"))
        metrics["mf_actor_entropy"] = to_np(torch.mean(actor_ent))
        with tools.RequiresGrad(self):
            if self.total_it % 2 == 0:
                metrics.update(self._actor_opt(actor_loss, self.actor.parameters()))
            metrics.update(self._value_opt_1(value_loss, self.value_1.parameters()))
            metrics.update(self._value_opt_2(value_loss, self.value_2.parameters()))
        return feat, state, action, weights, metrics

    # reuse feat_extractor

    def _data_sample(self, start, data):
        swap = lambda x: x.permute([1, 0] + list(range(2, len(x.shape))))
        start = {k: v[:,0] for k, v in start.items()}
        data = self._world_model.preprocess(data)
        rewards = data.pop('reward').unsqueeze(-1)
        discount = data.pop('discount')
        actions = data.pop('action')
        embed = self._world_model.encoder(data).detach()

        # def step(prev, _, embed, action, is_first):
        #     state, _, _ = prev
        #     latent = state
        #     feat = self._world_model.dynamics.get_feat(latent)
        #     # succ, _ = self._world_model.dynamics.obs_step(latent, action, embed, is_first)
        #     succ, _ = self._world_model.dynamics.observe()
        #     return succ, feat.detach(), action
        #     # return succ, feat, action


        # for i in range(self._config.batch_size):
        # succ, feats, actions = tools.static_scan(
        #     step, [torch.arange(self._config.batch_length), embed, action, data['is_first']], (start, None, None)
        # )
        # states = {k: swap(v.detach()) for k, v in succ.items()}
        # states = {k: swap(v) for k, v in succ.items()}
        # feats = swap(feats)
        # actions = swap(actions)

        states, _ = self._world_model.dynamics.observe(embed, actions, data['is_first'])
        feats = self._world_model.dynamics.get_feat(states)

        noise = (torch.rand_like(actions) * 0.2).clamp(-0.5, 0.5)
        noise_actions = self.actor(feats).sample() + noise
        noise_states = self._world_model.dynamics.img_step(states, noise_actions)
        next_feats = self._world_model.dynamics.get_feat(noise_states)

        feats = swap(feats)
        next_feats = swap(next_feats)
        actions = swap(actions)
        states = {k: swap(v) for k, v in states.items()}
        rewards = swap(rewards)
        discount = swap(discount)

        feats = feats.detach()
        states = {k: v.detach() for k, v in states.items()}

        # feats = feats[:-1]
        # next_feats = next_feats[1:]
        # actions = actions[:-1]
        # states = {k: v[:-1]for k, v in states.items()}
        # rewards = rewards[:-1]
        # discount = discount[:-1]

        return feats, next_feats, states, actions, rewards, discount

    def _compute_target(self, feat, state, reward, discount):
        value_1 = self.value_1(feat).mode()
        value_2 = self.value_2(feat).mode()
        value = torch.min(value_1, value_2)
        target = tools.lambda_return(
            reward[1:],
            value[:-1],
            discount[1:],
            bootstrap=value[-1],
            lambda_=self._config.discount_lambda,
            axis=0,
        )
        weights = torch.cumprod(
            torch.cat([torch.ones_like(discount[:1]), discount[:-1]], 0), 0
        ).detach()
        return target, weights, value[:-1]

    def _compute_actor_loss(
        self,
        feat,
        action,
        target,
        weights,
        base,
        state,
    ):
        metrics = {}
        inp = feat.detach()
        policy = self.actor(inp)
        # Q-val for actor is not transformed using symlog
        target = torch.stack(target, dim=1)
        if self._config.reward_EMA:
            offset, scale = self.reward_ema(target, self.ema_vals)
            normed_target = (target - offset) / scale
            normed_base = (base - offset) / scale
            adv = normed_target - normed_base
            metrics.update(tools.tensorstats(normed_target, "mf_normed_target"))
            metrics["mf_EMA_005"] = to_np(self.ema_vals[0])
            metrics["mf_EMA_095"] = to_np(self.ema_vals[1])

        if self._config.mf_gradient == "dynamics":
            actor_target = adv
            actor_loss = -weights[:-1] * actor_target
        elif self._config.mf_gradient == "reinforce":
            actor_target = (
                policy.log_prob(action)[:-1][:, :, None]
                * (target - self.value_1(feat[:-1]).mode()).detach()
            )
            actor_loss = -weights[:-1] * actor_target
        elif self._config.mf_gradient == "both":
            actor_target = (
                policy.log_prob(action)[:-1][:, :, None]
                * (target - self.value_1(feat[:-1]).mode()).detach()
            )
            mix = self._config.imag_gradient_mix
            actor_target = mix * target + (1 - mix) * actor_target
            metrics["mf_gradient_mix"] = mix
            actor_loss = -weights[:-1] * actor_target
        elif self._config.mf_gradient == "td3":
            pi = policy.sample()
            succ = self._world_model.dynamics.img_step(state, pi)
            succ_feat = self._world_model.dynamics.get_feat(succ)
            value = self.value_1(succ_feat).mean()
            actor_loss = -value[:-1]
            # actor_loss += torch.nn.functional.mse_loss(pi, action.detach())
        else:
            raise NotImplementedError(self._config.mf_gradient)
        return actor_loss, metrics

    # # reuse docoder only

    # def _data_sample(self, start, data):
    #     swap = lambda x: x.permute([1, 0] + list(range(2, len(x.shape))))
    #     start = {k: v[:,0] for k, v in start.items()}
    #     data = self._world_model.preprocess(data)
    #     rewards = data.pop('reward').unsqueeze(-1)
    #     discount = data.pop('discount')
    #     actions = data.pop('action')
    #     embed = self._world_model.encoder(data).detach()
    #     # embed = self._world_model.encoder(data)

    #     noise = (torch.rand_like(actions) * 0.2).clamp(-0.5, 0.5)
    #     next_actions = self.actor(embed).sample() + noise

    #     embed = swap(embed)
    #     actions = swap(actions)
    #     rewards = swap(rewards)
    #     discount = swap(discount)
    #     next_actions = swap(next_actions)


    #     return embed, actions, next_actions, rewards, discount

    # def _compute_target(self, embed, action, reward, discount):
    #     # if "cont" in self._world_model.heads:
    #     #     inp = self._world_model.dynamics.get_feat(imag_state)
    #     #     discount = self._config.discount * self._world_model.heads["cont"](inp).mean
    #     # else:
    #     #     discount = self._config.discount * torch.ones_like(reward)
    #     # discount = state.pop('discount')
    #     feat = torch.cat([embed, action], dim=-1)
    #     value_1 = self.value_1(feat).mode()
    #     value_2 = self.value_2(feat).mode()
    #     value = torch.min(value_1, value_2)
    #     target = tools.lambda_return(
    #         reward[1:],
    #         value[:-1],
    #         discount[1:],
    #         bootstrap=value[-1],
    #         lambda_=self._config.discount_lambda,
    #         axis=0,
    #     )
    #     weights = torch.cumprod(
    #         torch.cat([torch.ones_like(discount[:1]), discount[:-1]], 0), 0
    #     ).detach()
    #     return target, weights, value[:-1]

    # def _compute_actor_loss(
    #     self,
    #     embed,
    #     action,
    #     target,
    #     weights,
    #     base,
    # ):
    #     metrics = {}
    #     feat = torch.cat([embed, action], dim=-1)
    #     inp = embed.detach()
    #     policy = self.actor(inp)
    #     # Q-val for actor is not transformed using symlog
    #     target = torch.stack(target, dim=1)
    #     if self._config.reward_EMA:
    #         offset, scale = self.reward_ema(target, self.ema_vals)
    #         normed_target = (target - offset) / scale
    #         normed_base = (base - offset) / scale
    #         adv = normed_target - normed_base
    #         metrics.update(tools.tensorstats(normed_target, "mf_normed_target"))
    #         metrics["mf_EMA_005"] = to_np(self.ema_vals[0])
    #         metrics["mf_EMA_095"] = to_np(self.ema_vals[1])

    #     if self._config.mf_gradient == "dynamics":
    #         actor_target = adv
    #         actor_loss = -weights[:-1] * actor_target
    #     elif self._config.mf_gradient == "reinforce":
    #         actor_target = (
    #             policy.log_prob(action)[:-1][:, :, None]
    #             * (target - self.value_1(feat[:-1]).mode()).detach()
    #         )
    #         actor_loss = -weights[:-1] * actor_target
    #     elif self._config.mf_gradient == "both":
    #         actor_target = (
    #             policy.log_prob(action)[:-1][:, :, None]
    #             * (target - self.value_1(feat[:-1]).mode()).detach()
    #         )
    #         mix = self._config.imag_gradient_mix
    #         actor_target = mix * target + (1 - mix) * actor_target
    #         metrics["mf_gradient_mix"] = mix
    #         actor_loss = -weights[:-1] * actor_target
    #     elif self._config.mf_gradient == "td3":
    #         action = policy.sample()
    #         feat = torch.cat([embed, action], dim=-1)
    #         value = self.value_1(feat).mean()
    #         actor_loss = -value[:-1]
    #     else:
    #         raise NotImplementedError(self._config.mf_gradient)
    #     return actor_loss, metrics

    def _update_slow_target(self):
        if self._config.critic["slow_target"]:
            if self._updates % self._config.critic["slow_target_update"] == 0:
                mix = self._config.critic["slow_target_fraction"]
                for s, d in zip(self.value_1.parameters(), self._slow_value_1.parameters()):
                    d.data = mix * s.data + (1 - mix) * d.data
                for s, d in zip(self.value_2.parameters(), self._slow_value_2.parameters()):
                    d.data = mix * s.data + (1 - mix) * d.data
            self._updates += 1
