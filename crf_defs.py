from model_defs import *
from utils import *
from tensorflow.models.rnn.rnn_cell import *

###################################
# Building blocks                 #
###################################

# takes features and outputs potentials
def potentials_layer(in_layer, config, params, reuse=False, name='Potentials'):
    pot_size = config.n_tags ** config.pot_window
    out_shape = [batch_size, num_steps] + [config.n_tags] * config.pot_window
    batch_size = int(in_layer.get_shape()[0])
    num_steps = int(in_layer.get_shape()[1])
    input_size = int(in_layer.get_shape()[2])
    if reuse:
        tf.get_variable_scope().reuse_variables()
        W_pot = params.W_pot
        b_pot = params.b_pot
    else:
        W_pot = weight_variable([input_size, pot_size], name=name)
        b_pot = bias_variable([pot_size], name=name)
    flat_input = tf.reshape(in_layer, [-1, input_size])
    pre_scores = tf.matmul(flat_input, W_pot) + b_pot
    pots_layer = tf.reshape(pre_scores, [batch_size, num_steps, out_shape])
    return (pots_layer, W_pot, b_pot)


# pseudo-likelihood criterion
def pseudo_likelihood(potentials, pots_indices, target, config):
    batch_size = int(potentials.get_shape()[0])
    num_steps = int(potentials.get_shape()[1])
    pots_shape = map(int, potentials.get_shape()[2:])
    # move the current tag to the last dimension
    perm = range(len(potentials.get_shape()))
    mid = config.pot_window / 2
    perm[-1] = perm[-mid - 1]
    for i in range(-1, mid -1):
        perm[-mid + i] = perm[-mid + i] + 1
    perm_potentials = tf.transpose(potentials, perm=perm)
    # get conditional distribution of the current tag
    flat_pots = tf.reshape(perm_potentials, [-1, config.n_tags])
    flat_cond = tf.gather(flat_pots, pot_indices)
    pre_cond = tf.nn.softmax(flat_cond)
    conditional = tf.reshape(pre_cond, [batch_size, num_steps, -1])
    # compute pseudo-log-likelihood of sequence
    return -tf.reduce_sum(targets * tf.log(conditional))


# dynamic programming part 1: max sum
class CRFMaxCell(RNNCell):
    """Dynamic programming for CRF"""
    def __init__(self, config):
        self._num_units = config.n_tags ** (config.pot_window - 1)
        self.n_tags = config.n_tags
    
    @property
    def input_size(self):
        return self._num_units

    @property
    def output_size(self):
        return self._num_units
    
    @property
    def state_size(self):
        return self._num_units
    
    def __call__(self, inputs, state, scope=None):
        """Summation for dynamic programming. Inputs are the
        log-potentials. States are the results of the summation at the
        last step"""
        with tf.variable_scope(scope or type(self).__name__):
            # add states and log-potentials
            multiples = [1] * (len(state.get_shape()) + 1)
            multiples[-1] = self.n_tags
            exp_state = tf.tile(tf.expand_dims(state, -1), multiples)
            added = exp_state + inputs
            # return maxes, arg_maxes along first dimension (after the batch dim)
            new_state = tf.reduce_max(added, 1)
            max_id = tf.argmax(added, 1)
        return new_state, max_id


# max a posteriori tags assignment: implement dynamic programming
def map_assignment(potentials, config):
    batch_size = int(potentials.get_shape()[0])
    num_steps = int(potentials.get_shape()[1])
    pots_shape = map(int, potentials.get_shape()[2:])
    inputs_list = [tf.reshape(x, [batch_size] + pots_shape)
                   for x in tf.split(1, num_steps, potentials)]
    # forward pass
    max_cell = CRFMaxCell(config)
    max_ids = [0] * len(inputs_list)
    state = tf.convert_to_tensor(np.zeros(pots_shape[:-1]))
    for t, input_ in enumerate(inputs_list):
        state, max_id = max_cell(inputs_list[t], state)
        max_ids[t] = max_id
    # backward pass
    powers = tf.to_int64(map(float, range(batch_size))) * \
             (config.n_tags ** (config.pot_window - 1))
    outputs = [-1] * len(inputs_list)
    best_end = tf.argmax(tf.reshape(state, [batch_size, -1]), 1)
    current = best_end + powers
    for i, _ in enumerate(outputs):
        outputs[-1 - i] = tf.gather(tf.reshape(max_ids[-1 - i], [-1]), current)
        current = outputs[-1 - i] + powers
    map_tags = tf.transpose(tf.pack(outputs))
    return map_tags


# dynamic programming part 2: sum product
class CRFSumCell(RNNCell):
    """Dynamic programming for CRF"""
    def __init__(self, config):
        self._num_units = config.n_tags ** (config.pot_window - 1)
        self.n_tags = config.n_tags
    
    @property
    def input_size(self):
        return self._num_units

    @property
    def output_size(self):
        return self._num_units
    
    @property
    def state_size(self):
        return self._num_units
    
    def __call__(self, inputs, state, scope=None):
        """Summation for dynamic programming. Inputs are the
        log-potentials. States are the results of the summation at the
        last step"""
        with tf.variable_scope(scope or type(self).__name__):
            # add states and log-potentials
            multiples = [1] * (len(state.get_shape()) + 1)
            multiples[-1] = self.n_tags
            exp_state = tf.tile(tf.expand_dims(state, -1), multiples)
            added = exp_state + inputs
            # log-sum along first dimension (after the batch dim)
            max_val = tf.reduce_max(added)
            added_exp = tf.exp(added - max_val)
            summed_exp = tf.reduce_sum(added_exp, 1)
            new_state = tf.log(summed_exp) + max_val
        return new_state


# computing the log partition for a sequence of length config.num_steps
def log_partition(potentials, config):
    batch_size = int(potentials.get_shape()[0])
    num_steps = int(potentials.get_shape()[1])
    pots_shape = map(int, potentials.get_shape()[2:])
    inputs_list = [tf.reshape(x, [batch_size] + pots_shape)
                   for x in tf.split(1, num_steps, potentials)]
    # forward pass
    sum_cell = CRFSumCell(config)
    state = tf.convert_to_tensor(np.zeros(pots_shape[:-1]))
    partial_sums = [0] * len(inputs_list)
    for t, input_ in enumerate(inputs_list):
        state = sum_cell(inputs_list[t], state)
        partial_sums[t] = state
    # sum at the end
    max_val = tf.reduce_max(state)
    state_exp = tf.exp(state - max_val)
    log_part = tf.log(tf.reduce_sum(state_exp)) + max_val
    return log_part


# TODO: alpha-beta rec
def marginals(potentials, config):
    batch_size = int(potentials.get_shape()[0])
    num_steps = int(potentials.get_shape()[1])
    pots_shape = map(int, potentials.get_shape()[2:])
    inputs_list = [tf.reshape(x, [batch_size] + pots_shape)
                   for x in tf.split(1, num_steps, potentials)]
    # forward and backwar pass
    sum_cell_f = CRFSumCell(config)
    sum_cell_b = CRFSumCell(config)
    state_f = tf.convert_to_tensor(np.zeros(pots_shape[:-1]))
    state_b = tf.convert_to_tensor(np.zeros(pots_shape[:-1]))
    partial_sums_f = [0] * len(inputs_list)
    partial_sums_b = [0] * len(inputs_list)
    for t, _ in enumerate(inputs_list):
        state_f = sum_cell_f(inputs_list[t], state_f)
        partial_sums_f[t] = state_f
        state_b = sum_cell_b(inputs_list[t], state_b)
        partial_sums_b[-1 - t] = state_b
    # TODO: compute marginals
    marginals = 0
    return marginals


###################################
# Making a (deep) CRF             #
###################################

def make_crf(config, params, reuse=False, name='CRF'):
    with tf.variable_scope(name):
        pot_indices = tf.placeholder(tf.int32, [batch_size * num_steps])
        targets = tf.placeholder(tf.float32, [batch_size, num_steps, config.n_tags])
        if reuse:
            tf.get_variable_scope().reuse_variables()
        (input_ids, out_layer, embeddings) = feature_layer(config, params,
                                                           reuse=reuse)
        params.embeddings = embeddings
        if config.verbose:
            print('features layer done')
        # TODO: add layers
        (pots_layer, W_pot, b_pot) = potentials_layer(out_layer, config,
                                                      params)
        params.W_pot = W_pot
        params.b_pot = b_pot
        if config.verbose:
            print('potentials layer done')
        # TODO: switch criteria
        pseudo_ll = pseudo_likelihood(potentials, pots_indices, target,
                                      config)
        criterion = pseudo_ll
        # L1 regularization
        for feat in config.l1_list:
            criterion += config.l1_reg * \
                         tf.reduce_sum(tf.abs(params.embeddings[feat]))
        # compute map accuracy
        map_tags = map_assignment(potentials, config)
        correct_prediction = tf.equal(map_tags, tf.argmax(targets, 2))
        accuracy = tf.reduce_sum(tf.cast(correct_prediction,
                                 "float") * tf.reduce_sum(targets, 2)) /\
                  tf.reduce_sum(targets)
    return (input_ids, pots_indices, target, criterion, accuracy)
