import copy
import importlib
import os
import time

import numpy
import ray
import torch
from torch.utils.tensorboard import SummaryWriter

import models
import replay_buffer
import self_play
import shared_storage
import trainer


class MuZero:
    """
    Main class to manage MuZero.

    Args:
        game_name (str): Name of the game module, it should match the name of a .py file
        in the "./games" directory.

    Example:
        >>> muzero = MuZero("mario")
        >>> muzero.train()
        >>> muzero.load_model()
        >>> muzero.test()
    """

    def __init__(self, game_name):
        self.game_name = game_name

        # Load the game and the config from the module with the game name
        try:
            game_module = importlib.import_module("games." + self.game_name)
            self.config = game_module.MuZeroConfig()
            self.Game = game_module.Game
        except Exception as err:
            print(
                '{} is not a supported game name, try "cartpole" or refer to the documentation for adding a new game.'.format(
                    self.game_name
                )
            )
            raise err

        os.makedirs(os.path.join(self.config.results_path), exist_ok=True)

        # Fix random generator seed for reproductibility
        numpy.random.seed(self.config.seed)
        torch.manual_seed(self.config.seed)

        # Initial weights used to initialize components
        self.muzero_weights = models.MuZeroNetwork(
            self.config.observation_shape,
            self.config.stacked_observations,
            len(self.config.action_space),
            self.config.encoding_size,
            self.config.hidden_layers,
            self.config.support_size,
        ).get_weights()

    def train(self):
        ray.init()
        writer = SummaryWriter(
            os.path.join(self.config.results_path, self.game_name + "_summary")
        )

        # Initialize workers
        training_worker = trainer.Trainer.options(
            num_gpus=1 if "cuda" in self.config.training_device else 0
        ).remote(copy.deepcopy(self.muzero_weights), self.config)
        shared_storage_worker = shared_storage.SharedStorage.remote(
            copy.deepcopy(self.muzero_weights), self.game_name, self.config,
        )
        replay_buffer_worker = replay_buffer.ReplayBuffer.remote(self.config)
        self_play_workers = [
            self_play.SelfPlay.remote(
                copy.deepcopy(self.muzero_weights),
                self.Game(self.config.seed + seed),
                self.config,
            )
            for seed in range(self.config.num_actors)
        ]
        test_worker = self_play.SelfPlay.remote(
            copy.deepcopy(self.muzero_weights), self.Game(), self.config
        )

        # Launch workers
        [
            self_play_worker.continuous_self_play.remote(
                shared_storage_worker, replay_buffer_worker
            )
            for self_play_worker in self_play_workers
        ]
        test_worker.continuous_self_play.remote(shared_storage_worker, None, True)
        training_worker.continuous_update_weights.remote(
            replay_buffer_worker, shared_storage_worker
        )

        # Loop for monitoring in real time the workers
        print(
            "\nTraining...\nRun tensorboard --logdir ./ and go to http://localhost:6006/ to see in real time the training performance.\n"
        )
        counter = 0
        infos = ray.get(shared_storage_worker.get_infos.remote())
        try:
            while infos["training_step"] < self.config.training_steps:
                # Get and save real time performance
                infos = ray.get(shared_storage_worker.get_infos.remote())
                writer.add_scalar(
                    "1.Total reward/Total reward", infos["total_reward"], counter
                )
                writer.add_scalar(
                    "2.Workers/Self played games",
                    ray.get(replay_buffer_worker.get_self_play_count.remote()),
                    counter,
                )
                writer.add_scalar(
                    "2.Workers/Training steps", infos["training_step"], counter
                )
                writer.add_scalar("3.Loss/1.Total loss", infos["total_loss"], counter)
                writer.add_scalar("3.Loss/Value loss", infos["value_loss"], counter)
                writer.add_scalar("3.Loss/Reward loss", infos["reward_loss"], counter)
                writer.add_scalar("3.Loss/Policy loss", infos["policy_loss"], counter)
                print(
                    "Last test reward: {0:.2f}. Training step: {1}/{2}. Played games: {3}. Loss: {4:.2f}".format(
                        infos["total_reward"],
                        infos["training_step"],
                        self.config.training_steps,
                        ray.get(replay_buffer_worker.get_self_play_count.remote()),
                        infos["total_loss"],
                    ),
                    end="\r",
                )
                counter += 1
                time.sleep(3)
        except KeyboardInterrupt as err:
            # Comment the line below to be able to stop the training but keep running
            raise err
            pass
        self.muzero_weights = ray.get(shared_storage_worker.get_weights.remote())
        ray.shutdown()

    def test(self, render, muzero_player):
        """
        Test the model in a dedicated thread.

        Args:
            render : boolean to display or not the environment.

            muzero_player : Integer with the player number of MuZero in case of multiplayer
            games, None let MuZero play all players turn by turn.
        """
        print("\nTesting...")
        ray.init()
        self_play_workers = self_play.SelfPlay.remote(
            copy.deepcopy(self.muzero_weights), self.Game(), self.config
        )
        test_rewards = []
        for _ in range(self.config.test_episodes):
            history = ray.get(
                self_play_workers.play_game.remote(0, render, muzero_player)
            )
            test_rewards.append(sum(history.rewards))
        ray.shutdown()
        return test_rewards

    def load_model(self, path=None):
        if not path:
            path = os.path.join(self.config.results_path, self.game_name)
        try:
            self.muzero_weights = torch.load(path)
            print("Using weights from {}".format(path))
        except FileNotFoundError:
            print("There is no model saved in {}.".format(path))


if __name__ == "__main__":
    # Use the game and config from the ./games folder
    muzero = MuZero("mario")

    ## Train
    muzero.train()

    ## Test
    muzero.load_model()

    # Render some self-played games
    muzero.test(render=True, muzero_player=None)

    # Let user play against MuZero (MuZero is player 0 here)
    # muzero.test(render=True, muzero_player=0)
