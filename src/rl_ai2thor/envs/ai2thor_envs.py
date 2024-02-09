"""
Gymnasium interface for AI2THOR RL environment.

TODO: Finish module docstrings.
"""

from typing import Optional
import pathlib

import ai2thor.controller
from ai2thor.server import Event
import gymnasium as gym
import numpy as np
import yaml
from numpy.typing import ArrayLike

from rl_ai2thor.utils.ai2thor_types import EventLike
from rl_ai2thor.utils.config_handling import update_nested_dict, ROOT_DIR
from tasks import GraphTask, PlaceObject
from reward import GraphTaskRewardHandler
from actions import ACTION_CATEGORIES, ACTIONS_BY_NAME, ALL_ACTIONS, ACTIONS_BY_CATEGORY


# %% Environment definitions
class ITHOREnv(gym.Env):
    """
    Wrapper base class for iTHOR environment.
    """

    metadata = {
        "render_modes": ["human"],
        "render_fps": 30,
    }
    # TODO: Check if we keep this

    def __init__(
        self,
        custom_config: Optional[dict] = None,
    ) -> None:
        """
        Initialize the environment.

        Args:
            custom_config (dict): Dictionary whose keys will override the default config.
        """
        # === Get full config ===
        config_dir = pathlib.Path(ROOT_DIR, "config")
        with open(config_dir / "general.yaml", "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        # Merge environment mode config with general config
        # with open(f"{self.config['enviroment_mode']}.yaml", "r", encoding="utf-8") as f:
        with open(
            config_dir
            / "environment_modes"
            / f"{self.config['environment_mode']}.yaml",
            "r",
            encoding="utf-8",
        ) as f:
            enviroment_mode_config = yaml.safe_load(f)
        update_nested_dict(self.config, enviroment_mode_config)
        # Update config with user config
        if custom_config is not None:
            update_nested_dict(self.config, custom_config)

        # === Get action space ===
        self.action_availablities = {action.name: False for action in ALL_ACTIONS}
        # Update the available actions with the environment mode config
        for action_category in self.config["action_categories"]:
            if action_category in ACTION_CATEGORIES:
                if self.config["action_categories"][action_category]:
                    # Enable all actions in the category
                    for action in ACTIONS_BY_CATEGORY[action_category]:
                        self.action_availablities[action.name] = True
            else:
                raise ValueError(
                    f"Unknown action category {action_category} in environment mode config."
                )
        # Handle specific action cases
        if self.config["simple_movement_actions"]:
            self.action_availablities["MoveBack"] = False
            self.action_availablities["MoveLeft"] = False
            self.action_availablities["MoveRight"] = False
        if self.config["use_done_action"]:
            self.action_availablities["Done"] = True
        if (
            self.config["partial_openness"]
            and self.config["action_categories"]["open_close_actions"]
            and not self.config["discrete_actions"]
        ):
            self.action_availablities["OpenObject"] = False
            self.action_availablities["CloseObject"] = False

        # Create action space dictionary
        available_actions = [
            action_name
            for action_name, available in self.action_availablities.items()
            if available
        ]

        self.action_idx_to_name = dict(enumerate(available_actions))
        action_space_dict: dict[str, gym.Space] = {
            "action_index": gym.spaces.Discrete(len(self.action_idx_to_name))
        }
        if not self.config["discrete_actions"]:
            action_space_dict["action_parameter"] = gym.spaces.Box(
                low=0, high=1, shape=()
            )
        if not self.config["target_closest_object"]:
            action_space_dict["target_object_position"] = gym.spaces.Box(
                low=0, high=1, shape=(2,)
            )

        self.action_space = gym.spaces.Dict(action_space_dict)

        # === Get observation space ===
        controller_parameters = self.config["controller_parameters"]
        resolution = (
            controller_parameters["height"],
            controller_parameters["width"],
        )
        nb_channels = 1 if self.config["grayscale"] else 3
        self.observation_space = gym.spaces.Box(
            low=0, high=255, shape=(resolution[0], resolution[1], nb_channels)
        )

        # === Initialize ai2thor controller ===
        self.config["controller_parameters"]["agentMode"] = "default"
        self.controller = ai2thor.controller.Controller(
            **self.config["controller_parameters"],
        )

        # === Other attributes ===
        dummy_metadata = {
            "screenWidth": self.config["controller_parameters"]["width"],
            "screenHeight": self.config["controller_parameters"]["height"],
        }
        self.last_event = Event(dummy_metadata)
        # TODO: Check if this is correct ^
        self.task = None
        self.step_count = 0

    def step(self, action: dict) -> tuple[ArrayLike, float, bool, bool, dict]:
        """
        Take a step in the environment.

        Args:
            action (dict): Action to take in the environment.

        Returns:
            observation(ArrayLike): Observation of the environment.
            reward (float): Reward of the action.
            terminated (bool): Whether the agent reaches a terminal state (realized the task).
            truncated (bool): Whether the limit of steps per episode has been reached.
            info (dict): Additional information about the environment.
        """
        # === Get action name, parameters and ai2thor action ===
        if not self.action_space.contains(action):
            raise gym.error.InvalidAction(
                f"Action {action} is not contained in the action space"
            )
        action_name = self.action_idx_to_name[action["action_index"]]
        env_action = ACTIONS_BY_NAME[action_name]
        target_object_coordinates = action.get("target_object_position", None)
        action_parameter = action.get("action_parameter", None)

        # === Identify the target object if needed for the action ===
        failed_action_event = None
        if env_action.has_target_object:
            if self.config["target_closest_object"]:
                # Look for the closest operable object for the action
                visible_objects = [
                    obj for obj in self.last_event.metadata["objects"] if obj["visible"]
                ]
                object_required_property = env_action.object_required_property
                closest_operable_object, search_distance = None, np.inf
                for obj in visible_objects:
                    if (
                        obj[object_required_property]
                        and obj["distance"] < search_distance
                    ):
                        closest_operable_object = obj
                        search_distance = obj["distance"]
                if closest_operable_object is not None:
                    target_object_id = closest_operable_object["objectId"]
                else:
                    failed_action_event = env_action.fail_perform(
                        env=self,
                        error_message=f"No operable object found to perform action {action_name} in the agent's field of view.",
                    )
                    target_object_id = None
            else:
                query = self.controller.step(
                    action="GetObjectInFrame",
                    x=target_object_coordinates[0],
                    y=target_object_coordinates[1],
                    checkVisible=False,  # TODO: Check if the behavior is correct (object not detected if not visible)
                )
                if bool(query):
                    target_object_id = query.metadata["actionReturn"]
                else:
                    failed_action_event = env_action.fail_perform(
                        env=self,
                        error_message=f"No object found at position {target_object_coordinates} to perform action {action_name}.",
                    )
                    target_object_id = None
                    # TODO: Implement a range of tolerance
        else:
            target_object_id = None
        # === Perform the action ===
        if failed_action_event is None:
            new_event = env_action.perform(
                self,
                action_parameter=action_parameter,
                target_object_id=target_object_id,
            )
        else:
            new_event = failed_action_event
        new_event.metadata["target_object_id"] = target_object_id
        # TODO: Add logging of the event, especially when the action fails

        self.step_count += 1

        observation = new_event.frame
        reward, terminated, task_info = self.reward_handler.get_reward(new_event)

        truncated = self.step_count >= self.config["max_episode_steps"]
        info = {"metadata": new_event.metadata, "task_info": task_info}

        self.last_event = new_event

        return observation, reward, terminated, truncated, info

    # TODO: Adapt this with general task and reward handling
    def reset(self, seed: Optional[int] = None) -> tuple[ArrayLike, dict]:
        """
        Reset the environment.

        """
        print("Resetting environment and starting new episode")
        super().reset(seed=seed)

        # Setup the scene
        self.last_event = self.controller.reset()
        observation = self.last_event.frame  # type: ignore

        # TODO: Add scene id handling

        # Initialize the task and reward handler
        self.task = self._sample_task(self.last_event)
        self.reward_handler = GraphTaskRewardHandler(self.task)
        task_completion, task_info = self.reward_handler.reset(self.last_event)

        # TODO: Check if this is correct
        if task_completion:
            self.reset(seed=seed)

        self.step_count = 0
        info = {"metadata": self.last_event.metadata, "task_info": task_info}

        return observation, info

    def close(self):
        self.controller.stop()

    # TODO: Implement approprate task sampling
    def _sample_task(self, event: EventLike) -> GraphTask:
        """
        Sample a task for the environment.
        # TODO: Make it dependant on the scene..?
        """
        # Temporarily return only a PlaceObject task
        # Sample a receptacle and an object to place
        scene_pickupable_objects = [
            obj["objectType"] for obj in event.metadata["objects"] if obj["pickupable"]
        ]
        scene_receptacles = [
            obj["objectType"] for obj in event.metadata["objects"] if obj["receptacle"]
        ]
        object_to_place = np.random.choice(scene_pickupable_objects)
        receptacle = np.random.choice(scene_receptacles)

        return PlaceObject(
            placed_object_type=object_to_place,
            receptacle_type=receptacle,
        )


# %%