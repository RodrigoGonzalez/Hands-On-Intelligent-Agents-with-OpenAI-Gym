"""
OpenAI Gym compatible Driving simulation environment based on Carla.
Requires the system environment variable CARLA_SERVER to be defined and be pointing to the
CarlaUE4.sh file on your system. The default path is assumed to be at: ~/software/CARLA_0.8.2/CarlaUE4.sh
"""

import atexit
import os
import random
import signal
import subprocess
import time
import traceback
import json
import numpy as np
import gym
from gym.spaces import Box, Discrete, Tuple

# Set this to the path to your Carla binary
SERVER_BINARY = os.environ.get(
    "CARLA_SERVER", os.path.expanduser("~/software/CARLA_0.8.2/CarlaUE4.sh"))
assert os.path.exists(SERVER_BINARY), "CARLA_SERVER environment variable is not set properly. Please check and retry"
# Import Carla python client API funcs
from .carla.client import CarlaClient
from .carla.sensor import Camera
from .carla.settings import CarlaSettings
from .carla.planner.planner import Planner, REACH_GOAL, GO_STRAIGHT, \
    TURN_RIGHT, TURN_LEFT, LANE_FOLLOW

# Carla planner commands
COMMANDS_ENUM = {
    REACH_GOAL: "REACH_GOAL",
    GO_STRAIGHT: "GO_STRAIGHT",
    TURN_RIGHT: "TURN_RIGHT",
    TURN_LEFT: "TURN_LEFT",
    LANE_FOLLOW: "LANE_FOLLOW",
}

# Mapping from string repr to one-hot encoding index to feed to the model
COMMAND_ORDINAL = {
    "REACH_GOAL": 0,
    "GO_STRAIGHT": 1,
    "TURN_RIGHT": 2,
    "TURN_LEFT": 3,
    "LANE_FOLLOW": 4,
}

# Load scenario configuration parameters from scenarios.json
__location__ = os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))
scenario_config = json.load(open(os.path.join(__location__, "scenarios.json")))
city = scenario_config["city"][1]  # Town2
weathers = [scenario_config['Weather']['WetNoon'], scenario_config['Weather']['ClearSunset'] ]
scenario_config['Weather_distribution'] = weathers

# Default environment configuration
ENV_CONFIG = {
    "enable_planner": True,
    "use_depth_camera": False,
    "discrete_actions": True,
    "server_map": "/Game/Maps/" + city,
    "scenarios": [scenario_config["Lane_Keep_Town2"]],
    "framestack": 2,  # note: only [1, 2] currently supported
    "early_terminate_on_collision": True,
    "verbose": False,
    "render_x_res": 800,
    "render_y_res": 600,
    "x_res": 80,
    "y_res": 80,
    "seed": 1
}

live_carla_processes = set()  # To keep track of all the Carla processes we launch to make the cleanup easier
def cleanup():
    print("Killing live carla processes", live_carla_processes)
    for pgid in live_carla_processes:
        os.killpg(pgid, signal.SIGKILL)
atexit.register(cleanup)


class CarlaEnv(gym.Env):
    def __init__(self, config=ENV_CONFIG):
        """
        Carla Gym Environment class implementation. Creates an OpenAI Gym compatible driving environment based on
        Carla driving simulator.
        :param config: A dictionary with environment configuration keys and values
        """
        self.config = config
        self.city = self.config["server_map"].split("/")[-1]
        if self.config["enable_planner"]:
            self.planner = Planner(self.city)

        if config["use_depth_camera"]:
            image_space = Box(
                -1.0, 1.0, shape=(
                    config["y_res"], config["x_res"],
                    1 * config["framestack"]), dtype=np.float32)
        else:
            image_space = Box(
                0.0, 255.0, shape=(
                    config["y_res"], config["x_res"],
                    3 * config["framestack"]), dtype=np.float32)
        self.observation_space = Tuple(
            [image_space,
             Discrete(len(COMMANDS_ENUM)),  # next_command
             Box(-128.0, 128.0, shape=(2,), dtype=np.float32)])  # forward_speed, dist to goal

        self._spec = lambda: None
        self._spec.id = "Carla-v0"
        self._seed = ENV_CONFIG["seed"]

        self.server_port = None
        self.server_process = None
        self.client = None
        self.num_steps = 0
        self.total_reward = 0
        self.prev_measurement = None
        self.prev_image = None
        self.episode_id = None
        self.measurements_file = None
        self.weather = None
        self.scenario = None
        self.start_pos = None
        self.end_pos = None
        self.start_coord = None
        self.end_coord = None
        self.last_obs = None

    def init_server(self):
        print("Initializing new Carla server...")
        # Create a new server process and start the client.
        self.server_port = random.randint(10000, 60000)
        self.server_process = subprocess.Popen(
            [SERVER_BINARY, self.config["server_map"],
             "-windowed", "-ResX=400", "-ResY=300",
             "-carla-server",
             "-carla-world-port={}".format(self.server_port)],
            preexec_fn=os.setsid, stdout=open(os.devnull, "w"))
        live_carla_processes.add(os.getpgid(self.server_process.pid))

        for i in range(RETRIES_ON_ERROR):
            try:
                self.client = CarlaClient("localhost", self.server_port)
                return self.client.connect()
            except Exception as e:
                print("Error connecting: {}, attempt {}".format(e, i))
                time.sleep(2)