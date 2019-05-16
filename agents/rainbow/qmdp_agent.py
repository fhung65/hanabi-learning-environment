# coding=utf-8
##
# By Fan
##

"""Implementation of a low level QMDP agent adapted to the multiplayer setting."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools

from third_party.dopamine import checkpointer
import dqn_agent
import rainbow_agent
import gin.tf
import numpy as np
import prioritized_replay_memory
import tensorflow as tf

slim = tf.contrib.slim

import pdb

def print_state(state):
    """Print some basic information about the state."""
    print("")
    print("Current player: {}".format(state.cur_player()))
    print(state)

    # Example of more queries to provide more about this state. For
    # example, bots could use these methods to to get information
    # about the state in order to act accordingly.
    print("### Information about the state retrieved separately ###")
    print("### Information tokens: {}".format(state.information_tokens()))
    print("### Life tokens: {}".format(state.life_tokens()))
    print("### Fireworks: {}".format(state.fireworks()))
    print("### Deck size: {}".format(state.deck_size()))
    print("### Discard pile: {}".format(str(state.discard_pile())))
    print("### Player hands: {}".format(str(state.player_hands())))
    print("")

def parse_observations(observations, num_actions, obs_stacker):
  """ ORIGINAL PYHANABI FUNCTION with minor edits
  Deconstructs the rich observation data into relevant components.

  Args:
    observations: dict, containing full observations.
    num_actions: int, The number of available actions.
    obs_stacker: Observation stacker object.

  Returns:
    current_player: int, Whose turn it is.
    legal_moves: `np.array` of floats, of length num_actions, whose elements
      are -inf for indices corresponding to illegal moves and 0, for those
      corresponding to legal moves.
    observation_vector: Vectorized observation for the current player.
  """
  current_player = observations['current_player']
  current_player_observation = (
      observations['player_observations'][current_player])

  legal_moves = current_player_observation['legal_moves_as_int']
  legal_moves = run_experiment.format_legal_moves(legal_moves, num_actions)

  observation_vector = np.array(current_player_observation['vectorized'])

  #obs_stacker.add_observation(observation_vector, current_player)
  #observation_vector = obs_stacker.get_observation_stack(current_player)

  return current_player, legal_moves, observation_vector

def run_one_episode(agent, environment, obs_stacker):
  """Runs the agent on a single game of Hanabi in self-play mode.

  Args:
    agent: Agent playing Hanabi.
    environment: The Hanabi environment.
    obs_stacker: Observation stacker object.

  Returns:
    step_number: int, number of actions in this episode.
    total_reward: float, undiscounted return for this episode.
  """
  obs_stacker.reset_stack()
  observations = environment.reset()
  current_player, legal_moves, observation_vector = (
      parse_observations(observations, environment.num_moves(), obs_stacker))
  action = agent.begin_episode(current_player, legal_moves, observation_vector)

  is_done = False
  total_reward = 0
  step_number = 0

  has_played = {current_player}

  # Keep track of per-player reward.
  reward_since_last_action = np.zeros(environment.players)

  while not is_done:
    chosen_action = observations['player_observations'][current_player]['legal_moves'][
                                    np.arange(20)[legal_moves>=0].tolist().index(action)]
    #print(observations['player_observations'][current_player])
    print_state(environment.state)
    print('action: {}'.format(str(chosen_action)))
    observations, reward, is_done, _ = environment.step(action.item())

    modified_reward = max(reward, 0) if run_experiment.LENIENT_SCORE else reward
    total_reward += modified_reward

    reward_since_last_action += modified_reward

    print('modified reward: {0} \t total reward:{1}'.format(modified_reward, total_reward))

    step_number += 1
    if is_done:
      break
    current_player, legal_moves, observation_vector = (
        parse_observations(observations, environment.num_moves(), obs_stacker))
    if current_player in has_played:
      agent.debug_obs = observations['player_observations'][observations['current_player']]
      agent.debug_other_obs = observations['player_observations'][1-observations['current_player']]
      action = agent.step(reward_since_last_action[current_player],
                          current_player, legal_moves, observation_vector)
    else:
      # Each player begins the episode on their first turn (which may not be
      # the first move of the game).
      action = agent.begin_episode(current_player, legal_moves,
                                   observation_vector)
      has_played.add(current_player)

    # Reset this player's reward accumulator.
    reward_since_last_action[current_player] = 0

  agent.end_episode(reward_since_last_action)

  tf.logging.info('EPISODE: %d %g', step_number, total_reward)
  return step_number, total_reward

#@gin.configurable
class QMDPAgent(object):
  """A compact implementation of the multiplayer QMDP agent.
  This uses a pre-trained mdp and makes actions in an ipomdp setting
  by sampling to take an expectation of Q(s,a) w.r.t. the probability/belief
  of being in the current state, b(s).
  """

  def __init__(self,
               game,
               checkpoint_dir,
               checkpoint_file_prefix = 'ckpt', #see dqn agent
               agent_type='DQN',
               num_actions=None,
               observation_size=None,
               num_players=None,
               #num_atoms=51,
               #vmax=25.,
               gamma=0.99,
               update_horizon=1,
               min_replay_history=500,
               update_period=4,
               target_update_period=500,
               epsilon_train=0.0,
               epsilon_eval=0.0,
               epsilon_decay_period=1000,
               #learning_rate=0.000025,
               optimizer_epsilon=0.00003125,
               tf_device='/cpu:*'):
    """Initializes the agent and constructs its graph.

    Args:
      num_actions: int, number of actions the agent can take at any state.
      observation_size: int, size of observation vector.
      num_players: int, number of players playing this game.
      #num_atoms: Int, the number of buckets for the value function distribution.
      #vmax: float, maximum return predicted by a value distribution.
      gamma: float, discount factor as commonly used in the RL literature.
      update_horizon: int, horizon at which updates are performed, the 'n' in
        n-step update.
      min_replay_history: int, number of stored transitions before training.
      update_period: int, period between DQN updates.
      target_update_period: int, update period for the target network.
      epsilon_train: float, final epsilon for training.
      epsilon_eval: float, epsilon during evaluation.
      epsilon_decay_period: int, number of steps for epsilon to decay.
      #learning_rate: float, learning rate for the optimizer.
      optimizer_epsilon: float, epsilon for Adam optimizer.
      tf_device: str, Tensorflow device on which to run computations.
    """
    self.debug_obs = None # placeholder for observation
    self.debug_other_obs = None

    ### testing pretrained mdp with full knowledge
    self.debug_full_obs = False

    ### testing pretrained mdp with full knowledge plus some error
    self.debug_with_nearly_perfect_knowledge = False
    # [0-1] add bits from card knowledge with this chance
    # 0 is same as qmdp, 1 is same as debug_full_obs
    self.ERROR_RATE = 0.25

    if (agent_type == 'DQN'):
      self.pretrained_mdp = dqn_agent.DQNAgent(
          num_actions=num_actions,
          observation_size=observation_size,
          num_players=num_players,
          gamma=gamma,
          update_horizon=update_horizon,
          min_replay_history=min_replay_history,
          update_period=update_period,
          target_update_period=target_update_period,
          epsilon_train=epsilon_train,
          epsilon_eval=epsilon_eval,
          epsilon_decay_period=epsilon_decay_period,
          graph_template=dqn_agent.dqn_template,
          tf_device=tf_device)
    elif (agent_type == 'RAINBOW'):
      self.pretrained_mdp = rainbow_agent.RainbowAgent(
          num_actions=num_actions,
          observation_size=observation_size,
          num_players=num_players,
          gamma=gamma,
          update_horizon=update_horizon,
          min_replay_history=min_replay_history,
          update_period=update_period,
          target_update_period=target_update_period,
          epsilon_train=epsilon_train,
          epsilon_eval=epsilon_eval,
          epsilon_decay_period=epsilon_decay_period,
          tf_device=tf_device)


    ## Load Weights
    experiment_checkpointer = checkpointer.Checkpointer(
        checkpoint_dir, checkpoint_file_prefix)

    # Check if checkpoint exists. Note that the existence of checkpoint 0 means
    # that we have finished iteration 0 (so we will start from iteration 1).
    latest_checkpoint_version = checkpointer.get_latest_checkpoint_number(
        checkpoint_dir)
    #latest_checkpoint_version = 4050
    if latest_checkpoint_version >= 0:
      dqn_dictionary = experiment_checkpointer.load_checkpoint(
          latest_checkpoint_version)
      #if (not
      try:
        self.pretrained_mdp.unbundle(
          checkpoint_dir, latest_checkpoint_version, dqn_dictionary)
      except Exception as e:
        print('failed to load model from {0}, version {1}'.format(
                checkpoint_dir, latest_checkpoint_version))
        print(e)
        quit()
    print('loaded model from {0}, version {1}'.format(
                checkpoint_dir, latest_checkpoint_version))

    self.pretrained_mdp.eval_mode = True

    ## Define qmdp action selection for tensorflow
    #mdp = self.pretrained_mdp
    #mdp._q + mdp.legal_actions_ph


    ## load/calculate params from game for low level belief
    self.players = game.num_players()
    self.handsize = game.hand_size()

    self.colors = game.num_colors()
    self.ranks = game.num_ranks()
    self.infotokens = game.max_information_tokens()
    self.lifetokens = game.max_life_tokens()

    self.maxdeck = 0 # should be 50
    for card_i in range(self.colors):
        for rank_i in range(self.ranks):
            self.maxdeck += game.num_cards(card_i, rank_i)

    self.cardbits = self.colors * self.ranks # should be 25

    self.hands_bits = ((self.players-1) * self.handsize * self.cardbits + self.players)
    self.board_bits = self.hands_bits + (
                      self.maxdeck - self.players * self.handsize + # deck
                      self.colors * self.ranks +    # fireworks
                      self.infotokens +        # info tokens
                      self.lifetokens)         # life tokens
    self.discard_bits = self.board_bits + self.maxdeck
    self.action_bits = self.discard_bits + (self.players +
                                        4 +
                                        self.players +
                                        self.colors +
                                        self.ranks +
                                        self.handsize +
                                        self.handsize +
                                        self.cardbits
                                        + 2)
    #self.knowledge_bits = (self.action_bits +
    #                       self.players * self.handsize *
    #                        (self.cardbits + self.colors + self.ranks))
    # in case we need back compatibility with old full observation vector
    REDUNDANT = False
    if (REDUNDANT):
      self.num_knowledge_bits = (self.players * self.handsize *
                                (self.cardbits + self.colors + self.ranks))
    else:
      self.num_knowledge_bits = (self.players * self.handsize *
                                (self.cardbits))

    self.low_rank_end = 3
    self.mid_rank_end = 9
    self.cards_per_color = 10 # = 3 + 2 + 2 + 2 + 1

    self.card_totals = np.zeros((self.colors, self.ranks))
    self.card_totals[:,0] = 3
    self.card_totals[:,1:4] = 2
    self.card_totals[:,-1] = 1

  def individual_card_counts(self, obs):
    # TODO: unit test this
    '''
    from parse_observation:
    obs = current_player_observation['vectorized']
    '''
    hand    = obs[  :self.hands_bits - self.players]
    board   = obs[self.hands_bits  :self.board_bits]
    discard = obs[self.board_bits  :self.discard_bits]

    ## hands
    hands = np.array(hand).reshape(self.players-1, self.handsize,
                                    self.colors, self.ranks)
    cards_in_hands = np.sum(hands, axis = (0,1))

    ## board
    deck      = board[:self.maxdeck - self.players * self.handsize]
    fireworks = board[len(deck):len(deck) + self.colors * self.ranks]

    cards_remaining = sum(deck)
    cards_played = np.array(fireworks).reshape(self.colors,self.ranks)

    ## discard
    # each color in discard looks like this:
    # lll      h
    # 1100011101
    discarded = np.array(discard).reshape(self.colors,self.cards_per_color)
    cards_discarded = np.zeros((self.colors,self.ranks))
    # total low (3 of each)
    cards_discarded[:,0] = np.sum(discarded[:,:self.low_rank_end], axis=1)
    # total mid (2 of each)
    cards_discarded[:,1:4] = (  discarded[:,self.low_rank_end:
                                            self.mid_rank_end:2] +
                                discarded[:,self.low_rank_end+1:
                                            self.mid_rank_end+1:2])
    # total high (1 of each)
    cards_discarded[:,-1] = discarded[:,-1]

    cards_gone = cards_played + cards_discarded + cards_in_hands

    card_counts = (self.card_totals - cards_gone).astype(float)
    #prob = card_counts / (self.maxdeck - np.sum(cards_gone))
    #return prob
    return card_counts

  def reshape_hand(self, hand, num_players=1):
    '''
    hand should be an array of (total) length
      num_players x handsize x colors x ranks
    '''
    res = hand.reshape(num_players, self.handsize, self.colors, self.ranks)
    if (num_players == 0):
      return res[0]
    else:
      return res

  def extract_knowledge(self, knowledge_obs, current_player=True):
    '''
    knowledge_obs needs to be just the knowledge part of the obs vector
    return (by reference) a slice of obs corresponding to knowledge
    '''
    knowledge = knowledge_obs.reshape(self.players,
                                      self.handsize,
                                      self.cardbits +
                                        self.colors +
                                        self.ranks)
    # discard other player knowledge, and reveal history
    # (see hanabi_lib/canonical_encoders.cc)
    # what's left is our cards
    if (current_player):
      knowledge = self.reshape_hand(knowledge[0,:,:self.cardbits])
    else:
      knowledge = self.reshape_hand(knowledge[1:,:,:self.cardbits],
                                    num_players = self.players-1)
    return knowledge

  def card_counts(self, obs):
    '''
    from parse_observation:
    obs = current_player_observation['vectorized']
    '''
    counts = self.individual_card_counts(obs)
    knowledge = self.extract_knowledge(obs[self.action_bits:])
    # knowledge is a bitmask representing potential cards

    if (self.debug_with_nearly_perfect_knowledge):
      temp_knowledge = knowledge
      knowledge = np.zeros_like(knowledge)
      # add error bits
      knowledge_mask = (temp_knowledge >= 1)
      error_bits = (np.random.rand(np.sum(knowledge_mask)) < self.ERROR_RATE)
      knowledge[knowledge_mask] = error_bits
      # fill in true hand (using other player's observation)
      other_observation = self.debug_other_obs['vectorized']
      true_hand = np.array(other_observation[:self.hands_bits-2]).reshape(
                           self.players-1, self.handsize, self.colors, self.ranks)
      knowledge |= true_hand[0,...]

    all_counts = counts * knowledge
    #if self.debug_obs['deck_size'] < 2:
    #  pdb.set_trace()
    return all_counts

  def _sample_hand(self, observation, n_samples=25):
    slots = range(self.handsize)
    samples = self._sample_hand_slots(observation, slots, n_samples, samples_only=True)
    return samples

  def _sample_hand_slots(self, observation, slots, n_samples=25, samples_only=False):
    # TODO: unit test this
    ''' sample state
       - sample some using belief
       - encode into placeholder format
    '''
    # flatten array of each card in hand
    card_counts = self.card_counts(observation).reshape(self.handsize, -1)

    samples = []
    for sample_i in range(n_samples):
      counts = np.copy(card_counts)
      # sample directly using belief
      card_inds = []
      slot_inds = []
      joint_prob = 1.0
      # sample from most constrained card first
      slot_counts = np.sum(card_counts[slots], axis=1)
      for _, hand_i in sorted(zip(slot_counts,slots), key=lambda x:x[0]):
        card_count = counts[hand_i]
        if (np.sum(card_count > 0)):
          prob = card_count / (1.0 * np.sum(card_count))
          card_ind = np.random.choice(a = len(prob),
                                      #size = 1, default is single value
                                      replace = False,
                                      p = prob)
        else:
          #TODO: handle edge case when out of cards
          pdb.set_trace()

        card_prob = prob[card_ind]
        # update accumulators
        card_inds.append(card_ind)
        slot_inds.append(hand_i)
        joint_prob *= card_prob
        # subtract this card from the card counts of other slots
        # where they're positive
        # (subtracts from all slots, but we're not re-using any)
        counts[:,card_ind] -= (0 < counts[:,card_ind])
      if (samples_only):
        samples.append((np.array(slot_inds), np.array(card_inds)))
      else:
        samples.append((np.array(slot_inds), np.array(card_inds), joint_prob, counts))
    return samples

  def full_obs_vector(self, observation):
    '''
    returns the fully observable vector components with:
    - Board
      - Deck
      - Fireworks
      - Info tokens
      - Life tokens
    - Discards
    - Hands (or if not fully observed, card knowledge)
      - (without bits indicating which hints were given)
    '''
    knowledge = np.array(observation[self.action_bits:])
    knowledge = knowledge.reshape(self.players,
                                  self.handsize,
                                  self.cardbits + self.colors + self.ranks)
    no_reveal_hist = knowledge[:,:,:self.cardbits].flatten()
    knowledge_len = no_reveal_hist.shape[0]
    mdp_observation_vector = np.concatenate((
                       observation[self.hands_bits:self.discard_bits],
                       no_reveal_hist))

    return mdp_observation_vector

  def begin_episode(self, current_player, legal_actions, observation):
    mdp_observation_vector = self.full_obs_vector(observation)
    return self.pretrained_mdp.begin_episode(current_player, legal_actions,
                                             mdp_observation_vector)

  def end_episode(self, final_rewards):
    # DO NOTHING
    return

  def _select_action(self, observation, legal_actions):
    """Select an action from the set of allowed actions.

    Args:
      observation: `np.array`, the current observation.
      legal_actions: `np.array`, describing legal actions, with -inf meaning
        not legal.

    Returns:
      action: int, a legal action.
    """
    if (np.random.rand() <= self.pretrained_mdp.epsilon_eval):
      # Choose a random action with probability epsilon.
      legal_action_indices = np.where(legal_actions == 0.0)
      return np.random.choice(legal_action_indices[0])
    else:
      if (self.debug_full_obs):
        mdp_observation = self.full_obs_vector(observation)
        action = self.pretrained_mdp._select_action(mdp_observation, legal_actions)
      else:
        avg_q_a = self._expected_action_value(observation)
        action = np.argmax(avg_q_a + legal_actions)
      return action

  def _expected_action_value(self, observation):
    return self._expected_action_value_direct_prob_belief(observation,
                        n_samples = 125)
    #return self._expected_action_value_expect_each_card(observation,
    #                    n_samples = 1)

  def _expected_action_value_direct_prob_belief(self, observation, n_samples = 25):

    mdp = self.pretrained_mdp
    ## construct (most of) fully observable observation vector
    # after this, we'll fill in our sampled card knowledge
    mdp_observation_vector = self.full_obs_vector(observation)
    # get samples
    samples = self._sample_hand(observation, n_samples)
    avg_q_a = 0
    # fill in sampled card knowledge and compute q value
    obs = np.array(mdp_observation_vector)
    # pull out the card knowledge section
    # (which is the fully observed hand components)
    knowledge = self.reshape_hand(obs[-self.num_knowledge_bits:],
                                  num_players=self.players)
    hand = knowledge[0,...].reshape(self.handsize, -1)
    # fill in other players' knowledge too using their hands
    others_knowledge = knowledge[1:,...]
    others_knowledge[:] = self.reshape_hand(observation[:self.hands_bits-2],
                                            num_players=self.players-1)
    for slot_inds, card_inds in samples:
      hand[:] = 0
      hand[slot_inds, card_inds] = 1
      mdp.state[0, :, 0] = obs
      q_a_vec = mdp._sess.run(mdp._q, {mdp.state_ph: mdp.state})
      avg_q_a += q_a_vec
    avg_q_a /= len(samples)
    return avg_q_a

  def _expected_action_value_expect_each_card(self, observation, n_samples = 1):
    """Select an action from the set of allowed actions.

    for each card:
      - samples all other cards
      - calculates expectation over all values of this card

    Args:
      observation: `np.array`, the current observation.

    Returns:
      action: int, a legal action.
    """
    mdp = self.pretrained_mdp
    #mdp._q + mdp.legal_actions_ph

    ## construct (most of) fully observable observation vector
    # after this, we'll fill in our sampled card knowledge

    mdp_observation_vector = self.full_obs_vector(observation)


    avg_q_a = 0
    # fill in sampled card knowledge and compute q value
    obs = np.array(mdp_observation_vector)

    knowledge = self.reshape_hand(obs[-self.num_knowledge_bits:],
                                  num_players=self.players)
    hand = knowledge[0,...].reshape(self.handsize, -1)
    # fill in other players' knowledge too using their hands
    others_knowledge = knowledge[1:,...]
    others_knowledge[:] = self.reshape_hand(observation[:self.hands_bits-2],
                                            num_players=self.players-1)

    for hand_i in range(self.handsize):
      # TODO: unit test this
      slot_index = np.arange(self.handsize)
      slot_index = slot_index[slot_index != hand_i] # omit one card
      samples = self._sample_hand_slots(observation,
                                        slots = slot_index,
                                        n_samples = n_samples)

      for slot_inds, card_inds, joint_prob, counts in samples:
        # the returned counts are updated to remove those sampled
        card_count = counts[hand_i]
        prob = card_count / (1.0 * np.sum(card_count))
        for card_i in range(self.colors * self.ranks):
          hand[:] = 0
          hand[slot_inds, card_inds] = 1 # set sampled cards
          hand[hand_i, card_i] = 1 # set this card
          mdp.state[0, :, 0] = obs
          q_a_vec = mdp._sess.run(mdp._q, {mdp.state_ph: mdp.state})
          avg_q_a += q_a_vec * prob[card_i]
    avg_q_a /= len(samples * self.colors * self.ranks)
    return avg_q_a

  def step(self, reward, current_player, legal_actions, observation):
    self.action = self._select_action(observation, legal_actions)
    return self.action

if __name__ == '__main__':
  print('testing')
  # NOTE: run this after running: source setup_script.sh

  import run_experiment

  test = 'RAINBOW'

  config_path = ('/home/siyuan/Downloads/test/hanabi/nips2019/src/' +
                  'hanabi-learning-environment/agents/rainbow/configs/')

  if (test == 'RAINBOW'):
    run_experiment.load_gin_configs([config_path + 'hanabi_rainbow.gin'], [])
    CKPT_DIR = '/home/siyuan/Downloads/test/hanabi/non_redundant_knowledge/hanabi_rainbow_ckpt_5200'
  elif (test == 'DQN'):
    run_experiment.load_gin_configs([config_path + 'hanabi_dqn.gin'], [])
    CKPT_DIR = '/home/siyuan/Downloads/test/hanabi/dqn_base_dir/checkpoints'
  else:
    print('please set test in main')
    quit()

  environment = run_experiment.create_environment()
  obs_stacker = run_experiment.create_obs_stacker(environment, history_size=1)

  agent = QMDPAgent(game = environment.game,
                    checkpoint_dir = CKPT_DIR,
                    agent_type = test,
                    observation_size=obs_stacker.observation_size(),
                    num_actions=environment.num_moves(),
                    num_players=environment.players)

  res = run_one_episode(agent, environment, obs_stacker)

