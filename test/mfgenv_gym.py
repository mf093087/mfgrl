import json
from typing import Tuple

import gym
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns


class MfgEnv(gym.Env):
    metadata = {"render_modes": ["human"]}

    def __init__(self, env_config):
        """Initialize"""
        super().__init__()

        self.scale_costs = env_config["scale_costs"]
        self.stochastic = env_config["stochastic"]
        self.render_mode = env_config["render_mode"]

        self._setup_data(env_config["data_file"])

        # observation and action spaces
        obs_dim = 2 + self.BUFFER_SIZE * 6 + self.NUM_CFGS * 4
        self.observation_space = gym.spaces.Box(
            low=-np.inf, high=np.inf, shape=(obs_dim,)
        )
        self.action_space = gym.spaces.Discrete(self.NUM_CFGS + 1)

    def reset(self) -> Tuple[np.ndarray, dict]:
        """Resets environment.

        Args:
            seed (int, optional): Random seed for determinism. Defaults to None.

        Returns:
            Tuple[np.ndarray, dict]: Observation and info.
        """
        super().reset()

        self.episode_steps = 0

        # total reward
        self.total_rewards = 0

        # reset buffer
        self.buffer_idx = 0

        # reset environment state
        self._env_state = {
            # demand data
            "demand": self.DEMAND,
            "demand_time": self.DEMAND_TIME,
            # available resources data
            "incurred_costs": np.zeros(self.BUFFER_SIZE, dtype=np.float32),
            "recurring_costs": np.zeros(self.BUFFER_SIZE, dtype=np.float32),
            "production_rates": np.zeros(self.BUFFER_SIZE, dtype=np.float32),
            "setup_times": np.zeros(self.BUFFER_SIZE, dtype=np.float32),
            "cfgs_status": np.zeros(self.BUFFER_SIZE, dtype=np.float32),
            "produced_counts": np.zeros(self.BUFFER_SIZE, dtype=np.float32),
            # market data
            "market_incurring_costs": self.market_incurring_costs,
            "market_recurring_costs": self.market_recurring_costs,
            "market_production_rates": self.market_production_rates,
            "market_setup_times": self.market_setup_times,
        }
        # static state are used for stochastic operations
        self._static_state = {
            "recurring_costs": np.zeros(self.BUFFER_SIZE, dtype=np.float32),
            "production_rates": np.zeros(self.BUFFER_SIZE, dtype=np.float32),
        }

        if self.render_mode == "human":
            sns.set()
            self.fig, self.axes = plt.subplots(6, 2, figsize=(10, 7))
            self.fig.suptitle("Manufacturing Environment")
            self._render_frame(action=-1, reward=0)

        return self._get_obs()

    def step(self, action: int) -> Tuple[np.ndarray, float, bool, bool, dict]:
        """Performs one step in environment.
        Args:
            action (int): Action index.

        Returns:
            Tuple[np.ndarray, float, bool, bool, dict]:
                Observation, reward, terminated, truncated, info.
        """
        assert 0 <= action <= self.BUFFER_SIZE, "Invalid action"

        terminated = False
        if action < self.NUM_CFGS:
            if self.buffer_idx < self.BUFFER_SIZE:
                info = {"msg": f"Decision step. Purchase cfg: {action}"}
                reward = self.buy_cfg(cfg_id=action)
            else:
                info = {"msg": "Terminated. Tried to purchase when the buffer is full!"}
                reward = self.PENALTY
                terminated = True
        else:
            info = {"msg": "Continuing production"}
            reward = self.continue_production()

        # update environment
        if self.stochastic:
            self._imitate_market_uncertainties()
            self._imitate_production_uncertainties()

        # check for possible terminations after environment update
        if not terminated:
            if (self._env_state["demand"] > 0) and (
                (self._env_state["demand_time"] <= 0)
                or (self.episode_steps >= self.MAX_EPISODE_STEPS)
            ):
                # demand was not satisfied within given time limits
                info = {"msg": "Demand was not satisfied."}
                reward = self.PENALTY
                terminated = True
            elif (
                (self._env_state["demand"] <= 0)
                and (self._env_state["demand_time"] >= 0)
                and (self.episode_steps <= self.MAX_EPISODE_STEPS)
            ):
                info = {"msg": "Demand is satisfied"}
                terminated = True

        self.total_rewards += reward

        # render
        if self.render_mode == "human":
            self._render_frame(action=action, reward=reward)
            if terminated:
                plt.show(block=True)
                plt.close("all")

        return (
            self._get_obs(),
            reward,
            terminated,
            info,
        )

    def buy_cfg(self, cfg_id: int) -> float:
        """Buys new configuration.
        This does not update the environment's time.

        Args:
            cfg_id (int): The index of new configuration.

        Returns:
            float: Reward as the negative cost of configuration.
        """
        # calculate reward
        reward = -1.0 * self._env_state["market_incurring_costs"][cfg_id]

        # buy new configuration
        # update inucrred costs
        self._env_state["incurred_costs"][self.buffer_idx] = self._env_state[
            "market_incurring_costs"
        ][cfg_id]

        # update recurring costs
        self._env_state["recurring_costs"][self.buffer_idx] = self._env_state[
            "market_recurring_costs"
        ][cfg_id]
        self._static_state["recurring_costs"][self.buffer_idx] = self._env_state[
            "market_recurring_costs"
        ][cfg_id]

        # update production rates
        self._env_state["production_rates"][self.buffer_idx] = self._env_state[
            "market_production_rates"
        ][cfg_id]
        self._static_state["production_rates"][self.buffer_idx] = self._env_state[
            "market_production_rates"
        ][cfg_id]

        # update setup times
        self._env_state["setup_times"][self.buffer_idx] = self._env_state[
            "market_setup_times"
        ][cfg_id]

        # update cfgs status
        self._env_state["cfgs_status"][self.buffer_idx] = (
            1 / self._env_state["market_setup_times"][cfg_id]
        )

        # update production
        self._env_state["produced_counts"][self.buffer_idx] = 0

        # increment buffer idx
        self.buffer_idx += 1

        return reward * self.TRADEOFF

    def continue_production(self) -> float:
        """Continues production.
        This updates the environment's time.

        Returns:
            float: Reward as the sum of negative recurring costs.
        """
        # .astype(int) ensures that only ready machines contribute
        reward = -1.0 * np.sum(
            self._env_state["cfgs_status"].astype(int)
            * self._env_state["recurring_costs"]
        )

        # produce products with ready configurations
        self._env_state["produced_counts"] += (
            self._env_state["cfgs_status"].astype(int)
            * self._env_state["production_rates"]
        )

        # update cfgs status
        # update only ready or being prepared cfgs
        updates = np.ceil(self._env_state["cfgs_status"])
        # add small eps to deal with 0.999999xxx
        progress = [
            1 / st + 1e-9 if st != 0 else 0 for st in self._env_state["setup_times"]
        ]
        self._env_state["cfgs_status"] = np.clip(
            self._env_state["cfgs_status"] + updates * progress, a_min=0, a_max=1
        )

        # update observation
        self._env_state["demand"] = self.DEMAND - np.sum(
            self._env_state["produced_counts"].astype(int)
        )
        self._env_state["demand_time"] -= 1

        return reward * (1 - self.TRADEOFF)

    def encode_obs(self, obs: dict) -> np.ndarray:
        """Encodes observation dictionary into vector.

        Args:
            obs (dict): Observation dictionary.

        Returns:
            np.ndarray: Observation vector.
        """
        return np.concatenate(
            (
                [obs["demand"], obs["demand_time"]],
                obs["incurred_costs"],
                obs["recurring_costs"],
                obs["production_rates"],
                obs["setup_times"],
                obs["cfgs_status"],
                obs["produced_counts"],
                obs["market_incurring_costs"],
                obs["market_recurring_costs"],
                obs["market_production_rates"],
                obs["market_setup_times"],
            )
        ).astype(np.float32)

    def decode_obs(self, obs_vec: np.ndarray) -> dict:
        """Decodes observation vector into observation dictionary.

        Args:
            obs_vec (np.ndarray): Observation vector.

        Returns:
            dict: Observation dictionary.
        """
        obs_dict = {}
        obs_dict["demand"] = obs_vec[0]
        obs_dict["demand_time"] = obs_vec[1]

        start = 2
        obs_dict["incurred_costs"] = obs_vec[start : start + self.BUFFER_SIZE]

        start += self.BUFFER_SIZE
        obs_dict["recurring_costs"] = obs_vec[start : start + self.BUFFER_SIZE]

        start += self.BUFFER_SIZE
        obs_dict["production_rates"] = obs_vec[start : start + self.BUFFER_SIZE]

        start += self.BUFFER_SIZE
        obs_dict["setup_times"] = obs_vec[start : start + self.BUFFER_SIZE]

        start += self.BUFFER_SIZE
        obs_dict["cfgs_status"] = obs_vec[start : start + self.BUFFER_SIZE]

        start += self.BUFFER_SIZE
        obs_dict["produced_counts"] = obs_vec[start : start + self.BUFFER_SIZE]

        start += self.BUFFER_SIZE
        obs_dict["market_incurring_costs"] = obs_vec[start : start + self.NUM_CFGS]

        start += self.NUM_CFGS
        obs_dict["market_recurring_costs"] = obs_vec[start : start + self.NUM_CFGS]

        start += self.NUM_CFGS
        obs_dict["market_production_rates"] = obs_vec[start : start + self.NUM_CFGS]

        start += self.NUM_CFGS
        obs_dict["market_setup_times"] = obs_vec[start : start + self.NUM_CFGS]

        return obs_dict

    def _imitate_production_uncertainties(self):
        """Imitates fluctuating production uncertainties:
        1. Failure of configurations: -+ 10%
        2. Production output: -+10%
        3. Recurring cost: -+10%
        """
        # imitate failure of a random configuration with failure rate 10%
        # failure changes cfg_status from 1 to random value between 0.7 and 0.1
        # where 0.7 is a major failure, and the value close to 1 is a minor failure
        # select randomly one of the running configurations
        running_cfgs = np.where(self._env_state["cfgs_status"] == 1)[0]
        if len(running_cfgs) > 0:
            cfg_id = np.random.choice(running_cfgs)
            if np.random.uniform(0, 1) > 0.9:
                self._env_state["cfgs_status"][cfg_id] = np.random.uniform(0.7, 1)

        # imitate fluctuating production rates
        prs = self._static_state["production_rates"]
        self._env_state["production_rates"][prs > 0] = np.random.uniform(
            low=(
                self._static_state["production_rates"][prs > 0]
                - 0.1 * self._static_state["production_rates"][prs > 0]
            ),
            high=(
                self._static_state["production_rates"][prs > 0]
                + 0.1 * self._static_state["production_rates"][prs > 0]
            ),
        )

        # imitate fluctuating recurring costs
        rcs = self._static_state["recurring_costs"]
        self._env_state["recurring_costs"][rcs > 0] = np.random.uniform(
            low=(
                self._static_state["recurring_costs"][rcs > 0]
                - 0.1 * self._static_state["recurring_costs"][rcs > 0]
            ),
            high=(
                self._static_state["recurring_costs"][rcs > 0]
                + 0.1 * self._static_state["recurring_costs"][rcs > 0]
            ),
        )

    def _imitate_market_uncertainties(self):
        """Imitates fluctuating market properties with 10% uncertainty."""
        # incurring costs
        self._env_state["market_incurring_costs"] = np.random.uniform(
            low=self.market_incurring_costs - 0.1 * self.market_incurring_costs,
            high=self.market_incurring_costs + 0.1 * self.market_incurring_costs,
        )

        # recurring costs
        self._env_state["market_recurring_costs"] = np.random.uniform(
            low=self.market_recurring_costs - 0.1 * self.market_recurring_costs,
            high=self.market_recurring_costs + 0.1 * self.market_recurring_costs,
        )

        # production rates
        self._env_state["market_production_rates"] = np.random.uniform(
            low=self.market_production_rates - 0.1 * self.market_production_rates,
            high=self.market_production_rates + 0.1 * self.market_production_rates,
        )

        # setup times
        self._env_state["market_setup_times"] = np.random.uniform(
            low=self.market_setup_times - 0.1 * self.market_setup_times,
            high=self.market_setup_times + 0.1 * self.market_setup_times,
        )

    def _get_obs(self) -> np.ndarray:
        """Gets observation.

        Returns:
            np.ndarray: Observation vector.
        """
        return self.encode_obs(self._env_state)

    def _get_info(self) -> dict:
        """Gets environment information.

        Returns:
            dict: Information.
        """
        return {}

    def _setup_data(self, data_file: str):
        """Sets up the data.

        Args:
            data_file (str): The location of data file.
        """
        with open(data_file, "r") as f:
            data = json.load(f)

        # constants
        self.BUFFER_SIZE = 10
        self.DEMAND = data["demand"]
        self.DEMAND_TIME = data["demand_time"]
        self.MAX_INCURRING_COST = data["max_incurring_cost"]
        self.MAX_RECURRING_COST = data["max_recurring_cost"]
        self.TRADEOFF = data["tradeoff"]
        self.NUM_CFGS = len(data["configurations"])
        self.PENALTY = data["penalty"]
        self.MAX_EPISODE_STEPS = self.BUFFER_SIZE + self.DEMAND_TIME

        self.market_incurring_costs = np.array([], dtype=np.float32)
        self.market_recurring_costs = np.array([], dtype=np.float32)
        self.market_production_rates = np.array([], dtype=np.float32)
        self.market_setup_times = np.array([], dtype=np.float32)

        for v in data["configurations"].values():
            self.market_incurring_costs = np.append(
                self.market_incurring_costs, v["incurring_cost"]
            )
            self.market_recurring_costs = np.append(
                self.market_recurring_costs, v["recurring_cost"]
            )
            self.market_production_rates = np.append(
                self.market_production_rates, v["production_rate"]
            )
            self.market_setup_times = np.append(
                self.market_setup_times, v["setup_time"]
            )

        if self.scale_costs:
            self.market_incurring_costs = (
                self.market_incurring_costs / self.MAX_INCURRING_COST
            )
            self.market_recurring_costs = (
                self.market_recurring_costs / self.MAX_RECURRING_COST
            )

        # sanity check whether the problem is feasible
        idx = np.argmax(self.market_production_rates)
        assert (
            self.DEMAND_TIME - self.market_setup_times[idx]
        ) * self.market_production_rates[idx] * self.BUFFER_SIZE > self.DEMAND, (
            "Problem is not feasible. "
            "Demand will not be satisfied even in the best case."
        )

    def _render_frame(self, **kwargs):
        """Renders one step of environment."""
        for ax in self.axes.flatten():
            ax.clear()

        buffer_idxs = [f"B{i}" for i in range(self.BUFFER_SIZE)]
        market_cfgs = [f"Mfg{i}" for i in range(self.NUM_CFGS)]
        palette = sns.color_palette()

        # remaining demand and time
        text_kwargs = dict(ha="center", va="center", fontsize=12)
        self.axes[0, 0].text(
            0.5,
            0.5,
            f"Remaining demand: {self._env_state['demand']}. "
            f"Remaining time: {self._env_state['demand_time']}",
            **text_kwargs,
        )
        self.axes[0, 0].set_yticklabels([])
        self.axes[0, 0].set_xticklabels([])
        self.axes[0, 0].grid(False)

        # cost
        text_kwargs = dict(ha="center", va="center", fontsize=12)
        action = kwargs["action"]
        reward = kwargs["reward"]
        self.axes[1, 0].text(
            0.5,
            0.5,
            f"Action: {action}. Step reward: {reward:.2f}. "
            f" Total rewards: {self.total_rewards:.2f}",
            **text_kwargs,
        )
        self.axes[1, 0].set_yticklabels([])
        self.axes[1, 0].set_xticklabels([])
        self.axes[1, 0].grid(False)

        # plot incurred costs
        self.axes[2, 0].bar(
            market_cfgs, self._env_state["market_incurring_costs"], color=palette[3]
        )
        self.axes[2, 0].set_ylabel("£")
        self.axes[2, 0].set_ylim((0, 1))
        self.axes[2, 0].set_xticklabels([])
        self.axes[2, 0].set_title("Incurring costs (market)")

        # plot recurring costs
        self.axes[3, 0].bar(
            market_cfgs, self._env_state["market_recurring_costs"], color=palette[4]
        )
        self.axes[3, 0].set_ylabel("kWh")
        self.axes[3, 0].set_ylim((0, 1))
        self.axes[3, 0].set_xticklabels([])
        self.axes[3, 0].set_title("Recurring costs (market)")

        # plot production rates
        self.axes[4, 0].bar(
            market_cfgs, self._env_state["market_production_rates"], color=palette[5]
        )
        self.axes[4, 0].set_ylabel("p/h")
        self.axes[4, 0].set_xticklabels([])
        self.axes[4, 0].set_title("Production rates (market)")

        # plot setup times
        self.axes[5, 0].bar(
            market_cfgs, self._env_state["market_setup_times"], color=palette[6]
        )
        self.axes[5, 0].set_ylabel("h")
        self.axes[5, 0].set_title("Setup times (market)")
        self.axes[5, 0].set_xlabel("Available configs.")

        # plot cfgs statuses
        progress_colors = [
            palette[2] if p == 1 else palette[1] for p in self._env_state["cfgs_status"]
        ]
        self.axes[0, 1].bar(
            buffer_idxs, self._env_state["cfgs_status"] * 100, color=progress_colors
        )
        self.axes[0, 1].set_ylabel("%")
        self.axes[0, 1].set_ylim([0, 100])
        self.axes[0, 1].set_xticklabels([])
        self.axes[0, 1].set_title("Configurations status (buffer)")

        # plot produced counts
        self.axes[1, 1].bar(
            buffer_idxs, self._env_state["produced_counts"], color=palette[0]
        )
        self.axes[1, 1].set_ylabel("unit")
        self.axes[1, 1].set_ylim(bottom=0)
        self.axes[1, 1].set_xticklabels([])
        self.axes[1, 1].set_title("Production (buffer)")

        # plot incurred costs
        self.axes[2, 1].bar(
            buffer_idxs, self._env_state["incurred_costs"], color=palette[3]
        )
        self.axes[2, 1].set_ylabel("£")
        self.axes[2, 1].set_ylim((0, 1))
        self.axes[2, 1].set_xticklabels([])
        self.axes[2, 1].set_title("Incurred costs (buffer)")

        # plot recurring costs
        self.axes[3, 1].bar(
            buffer_idxs, self._env_state["recurring_costs"], color=palette[4]
        )
        self.axes[3, 1].set_ylabel("kWh")
        self.axes[3, 1].set_ylim((0, 1))
        self.axes[3, 1].set_xticklabels([])
        self.axes[3, 1].set_title("Recurring costs (buffer)")

        # plot production rates
        self.axes[4, 1].bar(
            buffer_idxs, self._env_state["production_rates"], color=palette[5]
        )
        self.axes[4, 1].set_ylabel("p/h")
        self.axes[4, 1].set_xticklabels([])
        self.axes[4, 1].set_title("Production rates (buffer)")

        # plot setup times
        self.axes[5, 1].bar(
            buffer_idxs, self._env_state["setup_times"], color=palette[6]
        )
        self.axes[5, 1].set_ylabel("h")
        self.axes[5, 1].set_title("Setup times (buffer)")
        self.axes[5, 1].set_xlabel("Available buffer")

        self.fig.canvas.draw()
        self.fig.canvas.flush_events()
        plt.tight_layout()
        plt.pause(0.1)