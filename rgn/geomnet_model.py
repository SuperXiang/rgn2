""" Geometric network model for protein structure prediction.

    In general, there is an implicit ordering of tensor dimensions that is respected throughout. It is:

        NUM_STEPS, BATCH_SIZE, NUM_DIHEDRALS, NUM_DIMENSIONS

    All tensors are assumed to have this orientation unless otherwise labeled.

"""

import os
import numpy as np
import tensorflow as tf
import tensorflow.contrib.layers as layers
import rnn_cell_extended
from tensorflow.contrib.cudnn_rnn.python.layers import cudnn_rnn
from tensorflow.contrib.cudnn_rnn.python.ops import cudnn_rnn_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import init_ops
from geom_ops import *
from net_ops import *
from utils import *
from glob import glob
from copy import deepcopy
from itertools import zip_longest



# Public interface

SCOPE = 'geomnet'
DUMMY_LOSS = -1.
PREFETCH_BUFFER = 10
LOSS_SCALING_FACTOR = 0.01 # this is to convert recorded losses to angstroms
NUM_DSSPS = 8
DSSP_CLASSES = ['L', 'H', 'B', 'E', 'G', 'I', 'T', 'S']


class GeomNetModel(object):
    """Geometric network model"""

    # static variable to control creation of new objects and starting the model
    _is_started = False
    _num_models = 0

    def __init__(self, mode, config):
        """ Sets up type of instance object and invokes TF graph creation function. """

        # make sure model hasn't been started, otherwise bail.
        if not GeomNetModel._is_started:
            # instance variables
            self.mode = mode
            self.config = deepcopy(config)

            # set up and expose appropriate methods based on mode (for initial state)
            if mode == 'training':
                self.start    = self._start
            else:
                self.evaluate = self._evaluate
                self.predict  = self._predict

            # process config for derived properties
            io = self.config.io
            arch = self.config.architecture
            reg = self.config.regularization
            curr = self.config.curriculum
            opt = self.config.optimization
            init = self.config.initialization

            # test for correct curriculum configuration
            if curr['mode'] is None and curr['behavior'] is not None:
                raise RuntimeError('Curriculum mode must be set when curriculum behavior is set.')
            elif curr['mode'] is not None and curr['behavior'] is None:
                raise RuntimeError('Curriculum behavior must be set when curriculum mode is set.')

            # model name
            if io['name'] is None:
                io['name'] = 'model_' + str(GeomNetModel._num_models)
                GeomNetModel._num_models = GeomNetModel._num_models + 1

            # alphabet-related
            arch['alphabet'] = np.loadtxt(io['alphabet_file'], delimiter = ',')[:, 6:] if io['alphabet_file'] is not None else None
            if arch['alphabet'] is not None: arch['alphabet_size'] = len(arch['alphabet']) # set alphabet size if implicit
            arch['single_or_no_alphabet'] = type(arch['alphabet_size']) is not list # having multiple alphabets is isomorphic to not reusing alphabet
            arch['is_alphabetized'] = 'alphabet' in arch['tertiary_output']

            # angularization
            arch['is_angularized'] = 'angular' in arch['tertiary_output']

            # optimization
            if opt['optimizer'] == 'adadelta':
                opt.update({'rho': opt['decay']})

            # initialization
            if arch['higher_order_layers']:
                for key in ['recurrent_init']:
                    if type(init[key]) is not list: init[key] = [init[key]] * len(arch['recurrent_layer_size'])

            if arch['recurrent_nonlinear_out_proj_size'] is not None:
                for key in ['recurrent_nonlinear_out_proj_init']:
                    if type(init[key]) is not list: init[key] = [init[key]] * len(arch['recurrent_nonlinear_out_proj_size'])

            # architecture
            for key in ['attention',
                        'attention_mlp_size',
                        'recurrent_attention',
                        'recurrent_attention_length',
                        'recurrent_attention_output_proj_size',
                        'recurrent_attention_mlp_size',
                        'recurrent_attention_input_proj',
                        'recurrent_attention_input_proj_size']:
                if type(arch[key]) is not list: arch[key] = [arch[key]] * len(arch['recurrent_layer_size'])
            
            # regularization
            for key in ['recurrent_input_keep_probability', 
                        'recurrent_output_keep_probability', 
                        'recurrent_keep_probability',
                        'recurrent_state_zonein_probability',
                        'recurrent_memory_zonein_probability',
                        'alphabet_keep_probability',
                        'alphabet_normalization',
                        'recurrent_output_batch_normalization',
                        'recurrent_output_layer_normalization']:
                if type(reg[key]) is not list: reg[key] = [reg[key]] * len(arch['recurrent_layer_size'])

            # create graph
            self._create_graph(mode, self.config)

        else:
            raise RuntimeError('Model already started; cannot create new objects.')

    def _create_graph(self, mode, config):
        """ Creates TensorFlow computation graph 

            Creates a different model depending on whether mode is set to 'training' or 'evaluation'.
            The semantics are such that the head (default 'training' mode) model is the one
            required for starting, training, and checkpointing. Additionally the user may create any 
            number of 'evaluation' models that depend on the head model, but supplement it with 
            additional data sets (and different model semantics (e.g. no dropout)) for the evaluation 
            and logging of their performance. However a head model is always required, and it is the 
            only one that exposes the core methods for starting and training.

            Note that the head model creates all variables, even ones it doesn't use, because it is 
            the one with the reuse=None semantics. Ops however are specific to each model type and
            so some ops are missing from the training model and vice-versa.

            Almost all graph construction is done in this function, which relies on a number of private
            methods to do the actual construction. Methods internal to this class are ad hoc and thus
            not meant for general use--general methods are placed in separate *_ops python modules.
            Some parts of graph construction, namely summary ops, are done in the start method, to
            ensure that all models have been created.

            There are two types of internal (private, prefaced with _) variables stored in each
            object. One are ops collections, like training_ops, evaluation_ops, etc. These are lists 
            of ops that are run when the similarly named object method is called. As the graph is 
            built up, ops are added to these lists. The second type of variable are various nodes
            that are like TF methods, like the initializer, saver, etc, which are stored in the
            object and are accessed by various methods when necessary.
        """

        # set up appropriate op collections based on mode (for initial state)
        if mode == 'training':
            self._training_ops        = training_ops        = {} # collection of ops to be run at each step of training
            self._diagnostic_ops      = diagnostic_ops      = {} # collection of ops for diagnostics like weight norms and curriculum quantiles
        else:
            self._evaluation_ops      = evaluation_ops      = {} # collection of ops for evaluation of losses
            self._last_evaluation_ops = last_evaluation_ops = {} # collection of ops for the last evaluation in a multi-invocation evaluation
            self._prediction_ops      = prediction_ops      = {} # collection of ops for prediction of structures

        # set variable scoping, op scoping, and place on appropriate device
        with tf.variable_scope(SCOPE, reuse=(mode == 'evaluation')) as scope, \
             tf.name_scope(SCOPE + '/' + config.io['name'] + '/'), \
             tf.device(_device_function_constructor(**{k: config.computing[k] for k in ('functions_on_devices', 'default_device')})):

            # set graph seed
            if mode == 'training': tf.set_random_seed(config.initialization['graph_seed'])

            # Create curriculum state and tracking variables if needed.
            if config.curriculum['mode'] is not None:
                # Variable to hold current curriculum iteration
                curriculum_step = tf.get_variable(name='curriculum_step', shape=[], trainable=False, 
                                                  initializer=tf.constant_initializer(config.curriculum['base']))
                if mode == 'training': diagnostic_ops.update({'curriculum_step': curriculum_step})

            # Set up data ports
            if mode == 'training': self._coordinator = tf.train.Coordinator()
            if config.curriculum['mode'] == 'length':
                max_length = tf.cast(tf.reduce_min([curriculum_step, config.optimization['num_steps']]), tf.int32)
            else:
                max_length = config.optimization['num_steps']
            dataflow_config = merge_dicts(config.io, config.initialization, config.optimization, config.queueing)
            ids, primaries, evolutionaries, secondaries, tertiaries, masks, num_stepss = _dataflow(dataflow_config, max_length)

            # Set up inputs
            inputs = _inputs(merge_dicts(config.architecture, config.initialization), primaries, evolutionaries)

            # Compute dRMSD weights (this masks out meaningless (longer than sequence) pairwise distances and incorporates curriculum weights)
            weights_config = merge_dicts(config.optimization, config.curriculum, config.loss, config.io)
            weights, flat_curriculum_weights = _weights(weights_config, masks, curriculum_step if config.curriculum['mode'] == 'loss' else None)
            if mode == 'training' and config.curriculum['mode'] == 'loss': diagnostic_ops.update({'flat_curriculum_weights': flat_curriculum_weights})

            # create alphabet if needed and if it will be shared between layers, otherwise set to None so that _dihedrals takes care of it
            alphabet_config = merge_dicts(config.architecture, config.initialization)
            if alphabet_config['is_alphabetized'] and alphabet_config['single_or_no_alphabet']:
                alphabet = _alphabet(mode, alphabet_config)
                if mode == 'training' and config.io['log_alphabet']: diagnostic_ops.update({'alphabet': alphabet})
            else:
                alphabet = None


            # Choose a netwrok architecture: RNNs or Transformer

            for case in switch(config.architecture['internal_representation']):
                if case('transformer'): 

                    #Create transformer layer(s) that translate primary sequences into internal representation
                    transformer_config = merge_dicts(config.initialization, config.architecture, config.regularization, config.optimization)

                    inputs2 = tf.transpose(inputs, perm=[1,0,2])
                    recurrent_outputs = transformer._encoder_model(inputs2, transformer_config, mode)
                    recurrent_outputs = tf.transpose(recurrent_outputs, perm=[1,0,2])

                    # recurrent_states missing. To be added later.
                
                elif case('recurrent'):
                    # Create recurrent layer(s) that translate  primary sequences into internal representation
                    recurrence_config = merge_dicts(config.initialization, config.architecture, config.regularization, config.optimization, config.computing, config.io)

                    # inputs: [NUM_STEPS, BATCH_SIZE, RECURRENT_LAYER_SIZE]
                    # recurrent_outputs: [NUM_STEPS, BATCH_SIZE, RECURRENT_LAYER_SIZE]

                    recurrent_outputs, recurrent_states = _higher_recurrence(mode, recurrence_config, inputs, num_stepss, alphabet=alphabet)
                elif case('none'):
                    recurrent_outputs = inputs
                
                else:
                    raise ValueError('Not an available internal representation.')

            # Secondary and tertiary structure generation
            if config.loss['secondary_weight'] > 0:
                # Convert internal representation to dssp class activations
                dssps_config = merge_dicts(config.initialization, config.optimization, config.io)
                dssps = _dssps(dssps_config, recurrent_outputs)            

                if mode == 'evaluation': 
                    prediction_ops.update({'ids': ids, 'dssps': dssps, 'num_stepss': num_stepss, 'recurrent_states': recurrent_states})

            if config.loss['tertiary_weight'] > 0:
                # Convert internal representation to (thru some number of possible ways) to geometrical representations, e.g., dihedrals, Frenet geometry, rotations
                parameters_config = merge_dicts(config.initialization, config.optimization, config.architecture, config.regularization, config.io)
                parameters_config.update({k: parameters_config[k][-1] for k in ['alphabet_keep_probability',
                                                                              'alphabet_normalization']})
                if not parameters_config['single_or_no_alphabet']: parameters_config.update({'alphabet_size': parameters_config['alphabet_size'][-1]})
                parameters = _geometric_parametrization(mode, parameters_config, recurrent_outputs, alphabet=alphabet)

                # Convert geometrical representations into full 3D structures and compute dRMSDs
                coordinates = _coordinates(merge_dicts(config.computing, config.optimization, config.queueing, config.architecture), parameters)
                drmsds = _drmsds(merge_dicts(config.optimization, config.loss, config.io, config.architecture), coordinates, tertiaries, weights)

                if mode == 'evaluation': 
                    prediction_ops.update({'ids': ids, 'coordinates': coordinates, 'num_stepss': num_stepss, 'recurrent_states': recurrent_states})

            # Losses
            if config.loss['include']:
                filters = {grp: id_filter(ids, grp) for grp in config.io['evaluation_sub_groups']} if mode == 'evaluation' else {}
                filters.update({'all': tf.tile([True], tf.shape(ids))})

                for group_id, group_filter in filters.items():
                    with tf.variable_scope(group_id):
                        # Secondary loss
                        effective_secondary_loss = 0.
                        if config.loss['secondary_weight'] > 0:
                            # Compute secondary loss by comparing predicted DSSP classes to target secondary sequences
                            secondary_loss_config = merge_dicts(config.optimization, config.loss, config.io)
                            secondary_loss = _secondary_loss(secondary_loss_config, dssps, secondaries, masks, group_filter)
                            effective_secondary_loss = config.loss['secondary_weight'] * secondary_loss

                            # Evaluation-specific code for making actual DSSP predictions and computing (non-differentiable) accuracy
                            if mode == 'evaluation':
                                # Non-differentiable accuracy of secondary structures
                                secondary_accuracy_config = merge_dicts(config.loss, config.io)
                                secondary_accuracy = _secondary_accuracy(secondary_accuracy_config, dssps, secondaries, masks, group_filter)

                                # update op collections with relevant losses
                                evaluation_ops.update({'secondary_loss_'     + group_id: secondary_loss, \
                                                       'secondary_accuracy_' + group_id: secondary_accuracy})

                        # Tertiary loss
                        effective_tertiary_loss = 0.
                        if config.loss['tertiary_weight'] > 0:
                            if config.queueing['num_evaluation_invocations'] > 1 and mode == 'training':
                                raise RuntimeError('Cannot use multiple invocations with training mode.')
                            else:
                                # Compute tertiary loss quotient parts by reducing dRMSDs based on normalization behavior
                                tertiary_loss_numerator, tertiary_loss_denominator = _reduce_loss_quotient(merge_dicts(config.loss, config.io, config.optimization), 
                                                                                                           drmsds, masks, group_filter, 
                                                                                                           name_prefix='tertiary_loss')

                                # Handles multiple invocations and gracefully degrades for single invocations.
                                # Variables are created below _per_ evaluation model, which is a deviation from my general design
                                # the scope of those variables is the evaluation model's, _not_ the training model's as usual
                                tertiary_loss, min_loss_achieved, min_loss_op, update_accu_op, reduce_accu_op = _accumulate_loss(
                                                                                                 merge_dicts(config.io, config.queueing),
                                                                                                 tertiary_loss_numerator, tertiary_loss_denominator,
                                                                                                 name_prefix='tertiary_loss')

                                if mode == 'evaluation':
                                    evaluation_ops.update(     {'update_accumulator_'         + group_id + '_op': update_accu_op})
                                    last_evaluation_ops.update({'tertiary_loss_'              + group_id        : tertiary_loss * LOSS_SCALING_FACTOR, \
                                                                'reduce_accumulator_'         + group_id + '_op': reduce_accu_op, \
                                                                'min_tertiary_loss_achieved_' + group_id        : min_loss_achieved * LOSS_SCALING_FACTOR, \
                                                                'min_tertiary_loss_'          + group_id + '_op': min_loss_op})

                            if config.io['log_model_summaries']: tf.add_to_collection(config.io['name'] + '_tertiary_losses', tertiary_loss)
                            effective_tertiary_loss = config.loss['tertiary_weight'] * tertiary_loss

                        # Final loss and related housekeeping
                        loss = tf.add(effective_secondary_loss, effective_tertiary_loss, name='loss')
                        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS) # batch_norm related
                        if update_ops: loss = control_flow_ops.with_dependencies(tf.tuple(update_ops), loss)
                        if config.io['log_model_summaries']: tf.add_to_collection(config.io['name'] + '_losses', loss)
                        if group_id == config.curriculum['loss_history_subgroup']: curriculum_loss = loss

                # Curriculum loss history; not always used but design is much cleaner if always created.
                curriculum_loss_history = tf.get_variable(
                                              initializer=tf.constant_initializer([DUMMY_LOSS] * config.curriculum['change_num_iterations']), 
                                              shape=[config.curriculum['change_num_iterations']], trainable=False, name='curriculum_loss_history')
                if mode == 'evaluation' and config.curriculum['update_loss_history']:
                    update_curriculum_history_op = _history(config.io, curriculum_loss, curriculum_loss_history)
                    last_evaluation_ops.update({'update_curriculum_history_op': update_curriculum_history_op})

            # Training
            if mode == 'training':
                # get grads, training ops
                self._global_step, minimize_op, grads_and_vars_dict = _training(config.optimization, loss)
                self._grads_and_vars_length = len(grads_and_vars_dict) / 2

                # update relevant op dicts
                # update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
                # if update_ops: training_ops.update({'update_ops': tf.tuple(update_ops)})
                training_ops.update({'minimize_op': minimize_op, 'global_step': self._global_step, 'ids': ids})
                diagnostic_ops.update(grads_and_vars_dict)

            # Curriculum
            if mode == 'training' and config.curriculum['behavior'] in ['fixed_rate', 'loss_threshold', 'loss_change']:
                curriculum_update_op = _curriculum(config.curriculum, curriculum_step, curriculum_loss_history, [minimize_op])
                training_ops.update({'curriculum_update_op': curriculum_update_op})

    def _train(self, session):
        """ Performs one iteration of training and, if applicable, advances the curriculum. """

        training_dict = ops_to_dict(session, self._training_ops)

        return training_dict['global_step'], training_dict['ids']

    def _evaluate(self, session, pretty=True):
        """ Evaluates loss(es) and returns dicts with the relevant loss(es). """
        if GeomNetModel._is_started:
            # evaluate
            num_invocations = self.config.queueing['num_evaluation_invocations']
            for invocation in range(num_invocations):
                if invocation < num_invocations - 1:
                    evaluation_dict = ops_to_dict(session, self._evaluation_ops)
                else:
                    evaluation_dict = ops_to_dict(session, merge_dicts(self._evaluation_ops, self._last_evaluation_ops))

            # write event summaries to disk
            if self.config.io['log_model_summaries']:
                self._summary_writer.add_summary(evaluation_dict['merged_summaries_op'], global_step=evaluation_dict['global_step'])

            # remove non-user facing ops
            if pretty: [evaluation_dict.pop(k) for k in evaluation_dict.keys() if 'op' in k]

            return evaluation_dict

        else:
            raise RuntimeError('Model has not been started or has already finished.')

    def _predict(self, session):
        """ Predict 3D structures. """

        if GeomNetModel._is_started:
            # evaluate prediction dict
            prediction_dict = ops_to_dict(session, self._prediction_ops)

            # process secondary and tertiary sequences
            if 'dssps' in prediction_dict: prediction_dict['dssps'] = np.transpose(np.argmax(prediction_dict['dssps'], 2))
            if 'coordinates' in prediction_dict: prediction_dict['coordinates'] = np.transpose(prediction_dict['coordinates'], (1, 2, 0))

            # generate return dict
            predictions = {}
            for id_, num_steps, secondary, tertiary, recurrent_states in zip_longest(*[prediction_dict.get(key, []) 
                                                                                        for key in ['ids', 'num_stepss', 'dssps', 'coordinates', 'recurrent_states']]):
                prediction = {}

                if secondary is not None:
                    prediction.update({'secondary': ''.join([DSSP_CLASSES[i] for i in secondary[:num_steps]])})

                if tertiary is not None:
                    last_atom = (num_steps - self.config.io['num_edge_residues']) * self.config.architecture['num_atom_type']
                    prediction.update({'tertiary': tertiary[:, :last_atom]})

                prediction.update({'recurrent_states': recurrent_states})

                predictions.update({id_: prediction})

            return predictions

        else:
            raise RuntimeError('Model has not been started or has already finished.')

    def _diagnose(self, session, pretty=True):
        """ Compute and return diagnostic measurements like weight norms and curriculum quantiles. """

        diagnostic_dict = ops_to_dict(session, self._diagnostic_ops)

        # write event summaries to disk
        if self.config.io['log_model_summaries']:
            for op in ['merged_summaries_op', 'base_merged_summaries_op']:
                self._summary_writer.add_summary(diagnostic_dict[op], global_step=diagnostic_dict['global_step'])

        # compute max/min of vars and grads
        vars_ = [diagnostic_dict['v' + str(i)] for i in range(self._grads_and_vars_length)]
        grads = [diagnostic_dict['g' + str(i)] for i in range(self._grads_and_vars_length)]
        diagnostic_dict.update({'min_weight': np.min([np.min(var) for var in vars_]),
                                'max_weight': np.max([np.max(var) for var in vars_]),
                                'min_grad': np.min([np.min(grad) for grad in grads]),
                                'max_grad': np.max([np.max(grad) for grad in grads])})

        # compute curriculum quantiles if applicable.
        if self.config.curriculum['mode'] == 'loss':
            quantiles = cum_quantile_positions(diagnostic_dict['flat_curriculum_weights'])
            diagnostic_dict.update({'curriculum_quantiles': quantiles})
        elif self.config.curriculum['mode'] == 'length':
            diagnostic_dict.update({'curriculum_quantiles': float('nan')})

        # remove non-user facing ops and tensors
        if pretty:
            diagnostic_dict.pop('flat_curriculum_weights', None)
            for i in range(self._grads_and_vars_length):
                diagnostic_dict.pop('v' + str(i))
                diagnostic_dict.pop('g' + str(i))

        return diagnostic_dict

    def _start(self, evaluation_models, session=None, restore_if_checkpointed=True):
        """ Initializes model from scratch or loads state from disk.
            Must be run once (and only once) before model is used. """

        if not GeomNetModel._is_started:
            # Checkpointing. Must be done here after all models have been instantiated, because evaluation models may introduce additional variables
            self._saver = tf.train.Saver(max_to_keep=self.config.io['max_checkpoints'], 
                                         keep_checkpoint_every_n_hours=self.config.io['checkpoint_every_n_hours'])

            # variable tracking and summarization. it has to be done here after all models have been instantiated
            model_names = set([model.config.io['name'] for model in evaluation_models] + [self.config.io['name']])
            if self.config.io['log_model_summaries']:
                # add histogram and scalar summaries losses
                for model_name in model_names:
                    for coll in ['secondary_losses', 'secondary_accuracies', 'tertiary_losses', 'losses']:
                        for node in tf.get_collection(model_name + '_' + coll):
                            tf.summary.scalar(node.name, node, collections=[model_name + '_' + tf.GraphKeys.SUMMARIES])
                if self.config.io['detailed_logs']:
                    # additional detailed summaries for losses
                    for model_name in model_names:
                        for coll in ['scess', 'matches', 'drmsdss', tf.GraphKeys.ACTIVATIONS]:
                            for node_or_named_output in tf.get_collection(model_name + '_' + coll): 
                                if type(node_or_named_output) is tf.Tensor:
                                    tf.summary.histogram(node_or_named_output.name, node_or_named_output, 
                                                         collections=[model_name + '_' + tf.GraphKeys.SUMMARIES])
                                elif type(node_or_named_output) is layers.utils.NamedOutputs:
                                    tf.summary.histogram(node_or_named_output[1].name, node_or_named_output[1],
                                                         collections=[model_name + '_' + tf.GraphKeys.SUMMARIES])

                    # summaries for trainable variables and their activations
                    for var in tf.trainable_variables(): tf.summary.histogram(var.name, var)
                    layers.summarize_activations()                

                # add housekeeping training ops that merge and write summaries
                self._summary_writer = tf.summary.FileWriter(self.config.io['logs_directory'])
                self._diagnostic_ops.update({'global_step': self._global_step,
                                             'base_merged_summaries_op': tf.summary.merge_all(), # leftovers not covered by model-specific 'summaries'
                                             'merged_summaries_op': tf.summary.merge_all(self.config.io['name'] + '_' + tf.GraphKeys.SUMMARIES)})

                # ditto for evaluation models
                for model in evaluation_models:
                    if model.mode == 'evaluation':
                        model._summary_writer = self._summary_writer
                        model._last_evaluation_ops.update({
                                 'global_step': self._global_step,
                                 'merged_summaries_op': tf.summary.merge_all(model.config.io['name'] + '_' + tf.GraphKeys.SUMMARIES)})

            # start session with appropriate device settings if no Session is passed
            if self.config.computing['fill_gpu']:
                gpu_fraction = None
            else:
                gpu_fraction = self.config.computing['gpu_fraction']

            if session is None:
                session = tf.Session(config=tf.ConfigProto(
                                        allow_soft_placement=False,
                                        inter_op_parallelism_threads=self.config.computing['num_cpus'],
                                        intra_op_parallelism_threads=self.config.computing['num_cpus'],
                                        gpu_options=tf.GPUOptions(per_process_gpu_memory_fraction=gpu_fraction,
                                                                  allow_growth=self.config.computing['allow_gpu_growth'])))

            # retrieve latest checkpoint, if any
            latest_checkpoint = tf.train.latest_checkpoint(self.config.io['checkpoints_directory'])

            # restore latest checkpoint if found, initialize from scratch otherwise.
            if not restore_if_checkpointed or latest_checkpoint is None:
                tf.global_variables_initializer().run(session=session)
                tf.local_variables_initializer().run(session=session)
            else:
                self._saver.restore(session, latest_checkpoint)
                tf.local_variables_initializer().run(session=session)

            # start coordinator and queueing threads
            self._threads = tf.train.start_queue_runners(sess=session, coord=self._coordinator)
            GeomNetModel._is_started = True

            # expose new methods and hide old ones
            self.train        = self._train
            self.diagnose     = self._diagnose
            self.save         = self._save
            self.is_done      = self._is_done
            self.current_step = self._current_step
            self.finish       = self._finish
            del self.start

            return session

        else:
            raise RuntimeError('Model already started.')

    def _save(self, session):
        """ Checkpoints current model. """

        checkpoints_dir = self.config.io['checkpoints_directory']
        if not os.path.exists(checkpoints_dir): os.makedirs(checkpoints_dir)
        return self._saver.save(session, checkpoints_dir, global_step=self._global_step)

    def _is_done(self):
        """ Returns True if training is finished, False otherwise. """
        
        return self._coordinator.should_stop()

    def _current_step(self, session):
        """ Returns the current global step. """

        return session.run(self._global_step)

    def _finish(self, session, save=True, close_session=True, reset_graph=True):
        """ Instructs the model to shutdown. """

        self._coordinator.request_stop()
        self._coordinator.join(self._threads)
        
        if save: self.save(session)
        if self.config.io['log_model_summaries']: self._summary_writer.close()
        if close_session: session.close()
        if reset_graph: tf.reset_default_graph()
        
        GeomNetModel._num_models = 0
        GeomNetModel._is_started = False

        del self.train, self.diagnose, self.save, self.is_done, self.current_step, self.finish

### Private functions
# These functions are meant strictly for internal use by GeomNetModel, and are 
# generally quite ad hoc. For TF-based ones, they do not carry out proper scoping 
# of their internals, as what they produce is meant to be dropped in the main TF 
# graph. They are often stateful, producing TF variables that are used by other 
# parts of GeomNetModel. However their behavior is still transparent in the sense
# that they're only passed parameters, not actual TF nodes or ops, and return
# everything that needs to be acted upon by GeomNetModel. So they don't modify
# the state of anything that's passed to them.

def _device_function_constructor(functions_on_devices={}, default_device=''):
    """ Returns a device placement function to insure that each operation is placed on the most optimal device. """

    def device_function(op):
        # note that one can't depend on ordering of items in dicts due to their indeterminancy
        for device, funcs in functions_on_devices.items():
            if any(((func in op.name) or any(func in node.name for node in op.inputs)) for func in funcs):
                return device
        else:
            return default_device

    return device_function

def _dataflow(config, max_length):
    """ Creates TF queues and nodes for inputting and batching data. """

    # files
    if config['data_files'] is not None:
        files = config['data_files']
    else:
        files = glob(config['data_files_glob'])

    # files queue
    file_queue = tf.train.string_input_producer(
        files,
        num_epochs=config['num_epochs'],
        shuffle=config['shuffle'],
        seed=config['queue_seed'],
        capacity=config['file_queue_capacity'],
        name='file_queue')

    # read instance
    inputs = read_protein(file_queue, max_length, config['num_edge_residues'], config['num_evo_entries'])

    # randomization
    if config['shuffle']: # based on https://github.com/tensorflow/tensorflow/issues/5147#issuecomment-271086206
        dtypes = list(map(lambda x: x.dtype, inputs))
        shapes = list(map(lambda x: x.get_shape(), inputs))
        randomizer_queue = tf.RandomShuffleQueue(capacity=config['batch_queue_capacity'], min_after_dequeue=config['min_after_dequeue'], 
                                                 dtypes=dtypes, seed=config['queue_seed'], name='randomization_queue')
        randomizer_enqueue_op = randomizer_queue.enqueue(inputs)
        randomizer_qr = tf.train.QueueRunner(randomizer_queue, [randomizer_enqueue_op])
        tf.add_to_collection(tf.GraphKeys.QUEUE_RUNNERS, randomizer_qr)
        inputs = randomizer_queue.dequeue()
        for tensor, shape in zip(inputs, shapes): tensor.set_shape(shape)
    num_steps, keep = inputs[-2:]

    # bucketing
    if config['bucket_boundaries'] is not None:
        batch_fun = tf.contrib.training.bucket_by_sequence_length
        batch_kwargs = {'input_length': num_steps,
                        'bucket_boundaries': config['bucket_boundaries'], 
                        'capacity': config['batch_queue_capacity'] / config['batch_size']}
        sel_slice = 1
    else:
        batch_fun = tf.train.maybe_batch
        batch_kwargs = {'capacity': config['batch_queue_capacity']}
        sel_slice = slice(len(inputs) - 1)

    # batching
    inputs = batch_fun(tensors=list(inputs)[:-1], keep_input=keep, dynamic_pad=True, batch_size=config['batch_size'], 
                       name='batching_queue', **batch_kwargs)
    ids, primaries_batch_major, evolutionaries_batch_major, secondaries_batch_major, tertiaries_batch_major, masks_batch_major, num_stepss = \
        inputs[sel_slice]

    # transpose to time_step major
    primaries      = tf.transpose(primaries_batch_major,      perm=(1, 0, 2), name='primaries') 
                     # primary sequences, i.e. one-hot sequences of amino acids.
                     # [NUM_STEPS, BATCH_SIZE, NUM_AAS]

    evolutionaries = tf.transpose(evolutionaries_batch_major, perm=(1, 0, 2), name='evolutionaries') 
                     # evolutionary sequences, i.e. multi-dimensional evolutionary profiles of amino acid propensities.
                     # [NUM_STEPS, BATCH_SIZE, NUM_EVO_ENTRIES]

    secondaries    = tf.transpose(secondaries_batch_major,    perm=(1, 0),    name='secondaries') 
                     # secondary sequences, i.e. sequences of DSSP classes.
                     # [NUM_STEPS, BATCH_SIZE]

    tertiaries     = tf.transpose(tertiaries_batch_major,     perm=(1, 0, 2), name='tertiaries')
                     # tertiary sequences, i.e. sequences of 3D coordinates.
                     # [(NUM_STEPS - NUM_EDGE_RESIDUES) x NUM_ATOMS, BATCH_SIZE, NUM_DIMENSIONS]

    masks          = tf.transpose(masks_batch_major,          perm=(1, 2, 0), name='masks')
                     # mask matrix for each datum that masks meaningless distances.
                     # [NUM_STEPS - NUM_EDGE_RESIDUES, NUM_STEPS - NUM_EDGE_RESIDUES, BATCH_SIZE]

    # assign names to the nameless
    ids = tf.identity(ids, name='ids')
    num_stepss = tf.identity(num_stepss, name='num_stepss')

    return ids, primaries, evolutionaries, secondaries, tertiaries, masks, num_stepss

def _inputs(config, primaries, evolutionaries):
    """ Returns final concatenated input for use in recurrent layer. """

    inputs_list = ([primaries]                                          if config['include_primary']      else []) + \
                  ([evolutionaries * config['evolutionary_multiplier']] if config['include_evolutionary'] else [])

    if inputs_list is not []:
        inputs = tf.concat(inputs_list, 2, name='inputs')
                 # [NUM_STEPS, BATCH_SIZE, NUM_AAS or NUM_EVO_ENTRIES or NUM_AAS + NUM_EVO_ENTRIES]
    else:
        raise RuntimeError('Either primaries or evolutionaries (or both) must be used as inputs.')

    return inputs

def _weights(config, masks, curriculum_step=None):
    """ Returns dRMSD weights that mask meaningless (missing or longer than 
        sequence residues) pairwise distances and incorporate the state of 
        the curriculum to differentially weigh pairwise distances based on 
        their proximity. """

    if config['atoms'] == 'c_alpha':
        if config['mode'] != 'loss':
            # no loss-based curriculum, create fixed weighting matrix that weighs all distances equally. 
            # minus one factor is there because we ignore self-distances.
            flat_curriculum_weights = np.ones(config['num_steps'] - config['num_edge_residues'] - 1, dtype='float32')

        elif config['mode'] == 'loss' and curriculum_step is not None:
            # create appropriate weights based on curriculum parameters and current step.
            flat_curriculum_weights = curriculum_weights(base=curriculum_step, 
                                                         slope=config['slope'], 
                                                         max_seq_length=config['num_steps'] - config['num_edge_residues'])
        else:
            raise RuntimeError('Curriculum step tensor not supplied.')

        # weighting matrix for entire batch that accounts for curriculum weighting.
        unnormalized_weights = weighting_matrix(flat_curriculum_weights, name='unnormalized_weights')
                               # [NUM_STEPS - NUM_EDGE_RESIDUES, NUM_STEPS - NUM_EDGE_RESIDUES]

        # create final weights by multiplying with masks and normalizing.
        mask_length = tf.shape(masks)[0]
        unnormalized_masked_weights = masks * unnormalized_weights[:mask_length, :mask_length, tf.newaxis]
        masked_weights = tf.div(unnormalized_masked_weights, 
                                tf.reduce_sum(unnormalized_masked_weights, axis=[0, 1]), 
                                name='weights')

        return masked_weights, flat_curriculum_weights

    else:
        raise NotImplementedError('Model does not currently support anything other than C alpha atoms for the loss function.')

def _higher_recurrence(mode, config, inputs, num_stepss, alphabet=None):
    """ Higher-order recurrence that creates multiple layers, possibly with interleaving dihedrals and dssps  """

    # prep
    is_training = (mode == 'training')
    initial_inputs = inputs

    # input batch or layer normalization (stats are computed over all batches and timesteps, effectively flattened)
    # note that this is applied _before_ the affine transform, which is non-standard
    if config['input_batch_normalization']:
        initial_inputs = layers.batch_norm(initial_inputs, center=True, scale=True, decay=0.999, epsilon=0.001, is_training=tf.constant(is_training), 
                                           scope='input_batch_norm', outputs_collections=config['name'] + '_' + tf.GraphKeys.ACTIVATIONS)
    if config['input_layer_normalization']:
        initial_inputs = layers.layer_norm(initial_inputs, center=True, scale=True, 
                                           scope='input_layer_norm', outputs_collections=config['name'] + '_' + tf.GraphKeys.ACTIVATIONS)

    # check if it's a simple recurrence that is just a lower-order recurrence (include simple multilayers) or a higher-order recurrence.
    # higher-order recurrences always concatenate both directions before passing them on to the next layer, in addition to allowing
    # additional information to be incorporated in the passed activations, including dihedrals and DSSPs. The final output that's returned
    # by this function is always just the recurrent outputs, not the other information, which is only used in intermediate layers.
    if config['higher_order_layers']:
        # higher-order recurrence that concatenates both directions and possibly additional outputs before sending to the next layer.
        
        # prep
        layer_inputs = initial_inputs
        layers_recurrent_outputs = []
        layers_recurrent_states  = []
        num_layers = len(config['recurrent_layer_size'])
        residual_n = config['residual_connections_every_n_layers']
        residual_shift = config['first_residual_connection_from_nth_layer'] - 1

        # iteratively construct each layer
        for layer_idx in range(num_layers):
            with tf.variable_scope('layer' + str(layer_idx)):
                # prepare layer-specific config
                layer_config = deepcopy(config)
                layer_config.update({k: [config[k][layer_idx]] for k in ['recurrent_layer_size',
                                                                         'recurrent_input_keep_probability',
                                                                         'recurrent_output_keep_probability',
                                                                         'recurrent_keep_probability',
                                                                         'recurrent_state_zonein_probability',
                                                                         'recurrent_memory_zonein_probability',
                                                                         'recurrent_attention',
                                                                         'recurrent_attention_length',
                                                                         'recurrent_attention_output_proj_size',
                                                                         'recurrent_attention_mlp_size',
                                                                         'recurrent_attention_input_proj',
                                                                         'recurrent_attention_input_proj_size']})
                layer_config.update({k: config[k][layer_idx] for k in ['attention',
                                                                       'attention_mlp_size',
                                                                       'recurrent_output_batch_normalization',
                                                                       'recurrent_output_layer_normalization',
                                                                       'alphabet_keep_probability',
                                                                       'alphabet_normalization',
                                                                       'recurrent_init']})
                layer_config.update({k: (config[k][layer_idx] if not config['single_or_no_alphabet'] else config[k]) for k in ['alphabet_size']})

                # core lower-level recurrence
                layer_recurrent_outputs, layer_recurrent_states = _recurrence(mode, layer_config, layer_inputs, num_stepss)

                # residual connections (only for recurrent outputs; other outputs are maintained but not wired in a residual manner)
                # all recurrent layer sizes must be the same
                if (residual_n >= 1) and ((layer_idx - residual_shift) % residual_n == 0) and (layer_idx >= residual_n + residual_shift):  
                    layer_recurrent_outputs = layer_recurrent_outputs + layers_recurrent_outputs[-residual_n]
                    print('residually wired layer ' + str(layer_idx - residual_n + 1) + ' to layer ' + str(layer_idx + 1))

                # batch or layer normalization (stats are computed over all batches and timesteps, effectively flattened)
                # this will affect only recurrent outputs, including the last one that goes into the dihedrals (assuming it's set to true)
                # note that this is applied _before_ the affine transform, which is non-standard
                if layer_config['recurrent_output_batch_normalization']:
                    layer_recurrent_outputs = layers.batch_norm(layer_recurrent_outputs, center=True, scale=True, decay=0.999, epsilon=0.001, 
                                                                scope='recurrent_output_batch_norm', is_training=tf.constant(is_training),
                                                                outputs_collections=config['name'] + '_' + tf.GraphKeys.ACTIVATIONS)
                if layer_config['recurrent_output_layer_normalization']:
                    layer_recurrent_outputs = layers.layer_norm(layer_recurrent_outputs, center=True, scale=True, 
                                                                scope='recurrent_output_layer_norm', 
                                                                outputs_collections=config['name'] + '_' + tf.GraphKeys.ACTIVATIONS)

                # add to list of recurrent layers' outputs (needed for residual connection and some skip connections)
                layers_recurrent_outputs.append(layer_recurrent_outputs)
                layers_recurrent_states.append(layer_recurrent_states)

                # non-recurrent attention
                if layer_config['attention']:
                    attentions = _attention(layer_config, layer_recurrent_outputs)
                    layer_recurrent_outputs = tf.concat([layer_recurrent_outputs, attentions], 2)

                # intermediate recurrences, only created if there's at least one layer on top of the current one
                if layer_idx != num_layers - 1: # not last layer
                    layer_outputs = []

                    # DSSPs
                    if config['include_dssps_between_layers']:
                        layer_dssps = _dssps(layer_config, layer_recurrent_outputs)
                        layer_outputs.append(layer_dssps)

                    # dihedrals #Modification dihedrals to parameters
                    if config['include_parameters_between_layers']:
                        layer_parameters = _geometric_parametrization(mode, layer_config, layer_recurrent_outputs, alphabet=alphabet)
                        layer_outputs.append(layer_parameters)

                    # skip connections from all previous layers (these will not be connected to the final linear output layer)
                    if config['all_to_recurrent_skip_connections']:
                        layer_outputs.append(layer_inputs)

                    # skip connections from initial inputs only (these will not be connected to the final linear output layer)
                    if config['input_to_recurrent_skip_connections'] and not config['all_to_recurrent_skip_connections']:
                        layer_outputs.append(initial_inputs)

                    # recurrent state
                    if config['include_recurrent_outputs_between_layers']:
                        layer_outputs.append(layer_recurrent_outputs)

                    # feed outputs as inputs to the next layer up
                    layer_inputs = tf.concat(layer_outputs, 2)

        # if recurrent to output skip connections are enabled, return all recurrent layer outputs, otherwise return only last one.
        # always return all states.
        if config['recurrent_to_output_skip_connections']:
            if layer_config['attention']: layers_recurrent_outputs.append(attentions)
            return tf.concat(layers_recurrent_outputs, 2), tf.concat(layers_recurrent_states, 1)
        else:
            return layer_recurrent_outputs,                tf.concat(layers_recurrent_states, 1)
    else:
        # simple recurrence, including multiple layers that use TF's builtin functionality, call lower-level recurrence function
        return _recurrence(mode, config, initial_inputs, num_stepss)

def _recurrence(mode, config, inputs, num_stepss):
    """ Recurrent layer for transforming inputs (primary sequences) into an internal representation. """
    
    is_training = (mode == 'training')
    reverse = lambda seqs: tf.reverse_sequence(seqs, num_stepss, seq_axis=0, batch_axis=1) # convenience function for sequence reversal

    # create recurrent initialization dict
    if config['recurrent_init'] != None:
        recurrent_init = dict_to_inits(config['recurrent_init'], config['recurrent_seed'])
    else:
        for case in switch(config['recurrent_unit']):
            if case('LNLSTM'):
                recurrent_init = {'base': None, 'bias': None}
            elif case('CudnnLSTM') or case('CudnnGRU'):
                recurrent_init = {'base': dict_to_init({}), 'bias': None}
            else:
                recurrent_init = {'base': None, 'bias': tf.zeros_initializer()}

    # fused mode vs. explicit dynamic rollout mode
    if 'Cudnn' in config['recurrent_unit']:
        # cuDNN-based fusion; assumes all (lower-order) layers are of the same size (first layer size) and all input dropouts are the same 
        # (first layer one). Does not support peephole connections, and only supports input dropout as a form of regularization.
        layer_size = config['recurrent_layer_size'][0]
        num_layers = len(config['recurrent_layer_size'])
        input_keep_prob = config['recurrent_input_keep_probability'][0]

        for case in switch(config['recurrent_unit']):
            if case('CudnnLSTM'):
                cell = cudnn_rnn.CudnnLSTM
            elif case('CudnnGRU'):
                cell = cudnn_rnn.CudnnGRU

        if is_training and input_keep_prob < 1: # this layer is needed because cuDNN dropout only applies to inputs between layers, not the first inputs
            inputs = tf.nn.dropout(inputs, input_keep_prob, seed=config['dropout_seed'])

        if num_layers > 1: # strictly speaking this isn't needed, but it allows multiple cuDNN-based models to run on the same GPU when num_layers = 1
            dropout_kwargs = {'dropout': 1 - input_keep_prob, 'seed': config['dropout_seed']}
        else:
            dropout_kwargs = {}

        outputs = []
        states = []
        scopes = ['fw', 'bw'] if config['bidirectional'] else ['fw']
        for scope in scopes:
            with tf.variable_scope(scope):
                rnn = cell(num_layers=num_layers, num_units=layer_size, direction=cudnn_rnn_ops.CUDNN_RNN_UNIDIRECTION, 
                           kernel_initializer=recurrent_init['base'], bias_initializer=recurrent_init['bias'], **dropout_kwargs)
                inputs_directed = inputs if scope == 'fw' else reverse(inputs)
                outputs_directed, (_, states_directed) = rnn(inputs_directed, training=is_training)
                outputs_directed = outputs_directed if scope == 'fw' else reverse(outputs_directed)
                outputs.append(outputs_directed)
                states.append(states_directed)
        outputs = tf.concat(outputs, 2)
        states  = tf.concat(states, 2)[0]

    elif 'LSTMBlockFused' == config['recurrent_unit']:
        # TF-based fusion; assumes a single (for lower-order) layer of the size of the first layer
        # currently doesn't support any form of regularization
        # DEPRECATED: does not properly return states 
        layer_size = config['recurrent_layer_size'][0]

        outputs = []
        scopes = ['fw', 'bw'] if config['bidirectional'] else ['fw']
        for scope in scopes:
            with tf.variable_scope(scope, initializer=recurrent_init['base']):
                cell = tf.contrib.rnn.LSTMBlockFusedCell(num_units=layer_size, forget_bias=config['recurrent_forget_bias'],
                                                         use_peephole=config['recurrent_peepholes'], cell_clip=config['recurrent_threshold'])
                inputs_directed = inputs if scope == 'fw' else reverse(inputs)
                outputs_directed, _ = cell(inputs_directed, sequence_length=num_stepss, dtype=tf.float32)
                outputs_directed = outputs_directed if scope == 'fw' else reverse(outputs_directed)
                outputs.append(outputs_directed)
        outputs = tf.concat(outputs, 2)
        
    else:
        # TF-based dynamic rollout
        if config['bidirectional']:
            outputs, states = tf.nn.bidirectional_dynamic_rnn(cell_fw=_recurrent_cell(mode, config, recurrent_init, 'fw'), 
                                                         cell_bw=_recurrent_cell(mode, config, recurrent_init, 'bw'), 
                                                         inputs=inputs, time_major=True, sequence_length=tf.to_int64(num_stepss),
                                                         dtype=tf.float32, swap_memory=True, parallel_iterations=config['num_recurrent_parallel_iters'])
            outputs = tf.concat(outputs, 2)
            states  = tf.concat(states,  2)
                      # [NUM_STEPS, BATCH_SIZE, 2 x RECURRENT_LAYER_SIZE]
                      # outputs of recurrent layer over all time steps.        
        else:
            outputs, states = tf.nn.dynamic_rnn(cell=_recurrent_cell(mode, config, recurrent_init),
                                                inputs=inputs, time_major=True, sequence_length=num_stepss, 
                                                dtype=tf.float32, swap_memory=True, parallel_iterations=config['num_recurrent_parallel_iters'])
                              # [NUM_STEPS, BATCH_SIZE, RECURRENT_LAYER_SIZE]
                              # outputs of recurrent layer over all time steps.

        # add newly created variables to respective collections
        if is_training:
            for v in tf.trainable_variables():
                if 'rnn' in v.name and ('cell/kernel' in v.name): tf.add_to_collection(tf.GraphKeys.WEIGHTS, v)
                if 'rnn' in v.name and ('cell/bias'   in v.name): tf.add_to_collection(tf.GraphKeys.BIASES,  v)

    return outputs, states

def _recurrent_cell(mode, config, recurrent_init, name=''):
    """ create recurrent cell(s) used in RNN """

    is_training = (mode == 'training')

    # lower-order multilayer
    cells = []
    for layer_idx, (layer_size, input_keep_prob, output_keep_prob, keep_prob, hidden_state_keep_prob, memory_cell_keep_prob, \
        recur_attn, recur_attn_length, recur_attn_out_proj_size, recur_attn_mlp_size, recur_attn_in_proj, recur_attn_in_proj_size) \
        in enumerate(zip(
            config['recurrent_layer_size'], 
            config['recurrent_input_keep_probability'], 
            config['recurrent_output_keep_probability'],
            config['recurrent_keep_probability'],
            config['recurrent_state_zonein_probability'], 
            config['recurrent_memory_zonein_probability'],
            config['recurrent_attention'],
            config['recurrent_attention_length'],
            config['recurrent_attention_output_proj_size'],
            config['recurrent_attention_mlp_size'],
            config['recurrent_attention_input_proj'],
            config['recurrent_attention_input_proj_size'])):
    
        # set context
        with tf.variable_scope('sublayer' + str(layer_idx) + (name if name is '' else '_' + name), initializer=recurrent_init['base']):

            # create core cell
            for case in switch(config['recurrent_unit']):
                if case('Basic'):
                    cell = tf.nn.rnn_cell.BasicRNNCell(num_units=layer_size, reuse=(not is_training))
                elif case('GRU'):
                    cell = tf.nn.rnn_cell.GRUCell(num_units=layer_size, reuse=(not is_training))
                elif case('LSTM'):
                    cell = tf.nn.rnn_cell.LSTMCell(num_units=layer_size, use_peepholes=config['recurrent_peepholes'],
                                                   forget_bias=config['recurrent_forget_bias'], cell_clip=config['recurrent_threshold'], 
                                                   initializer=recurrent_init['base'], reuse=(not is_training))
                elif case('LNLSTM'):
                    cell = tf.contrib.rnn.LayerNormBasicLSTMCell(num_units=layer_size, forget_bias=config['recurrent_forget_bias'],
                                                                 layer_norm=config['recurrent_layer_normalization'],
                                                                 dropout_keep_prob=keep_prob, reuse=(not is_training))
                elif case('LSTMBlock'):
                    cell = tf.contrib.rnn.LSTMBlockCell(num_units=layer_size, forget_bias=config['recurrent_forget_bias'], 
                                                        use_peephole=config['recurrent_peepholes'])

            # wrap cell with zoneout
            if hidden_state_keep_prob < 1 or memory_cell_keep_prob < 1:
                cell = rnn_cell_extended.ZoneoutWrapper(cell=cell, is_training=is_training, seed=config['zoneout_seed'],
                                                        hidden_state_keep_prob=hidden_state_keep_prob, memory_cell_keep_prob=memory_cell_keep_prob)

            # if not just evaluation, then wrap cell in dropout
            if is_training and (input_keep_prob < 1 or output_keep_prob < 1 or keep_prob < 1):
                cell = tf.nn.rnn_cell.DropoutWrapper(cell=cell, input_keep_prob=input_keep_prob, output_keep_prob=output_keep_prob, 
                                                     state_keep_prob=keep_prob, variational_recurrent=config['recurrent_variational_dropout'], 
                                                     seed=config['dropout_seed'])

            # attention CURRENTLY DEPRECATED
            # if recur_attn:
            #     if recur_attn_length is None: recur_attn_length = config['num_steps']
            #     recurrent_attn_init = dict_to_inits(config['recurrent_attention_init'], config['recurrent_attention_seed'])
            #     cell = tf.contrib.rnn.AttentionCellWrapper(cell=cell, attn_length=recur_attn_length, attn_size=recur_attn_out_proj_size, 
            #                                                attn_vec_size=recur_attn_mlp_size, input_proj=recur_attn_in_proj, 
            #                                                input_size=recur_attn_in_proj_size, state_is_tuple=True,
            #                                                input_proj_initializer=recurrent_attn_init['in_proj'], 
            #                                                output_proj_initializer=recurrent_attn_init['out_proj'], 
            #                                                attn_mlp_initializer=recurrent_attn_init['attn_mlp'])

            # add to collection
            cells.append(cell)

    # stack multiple cells if needed
    if len(cells) > 1:
        cell = tf.nn.rnn_cell.MultiRNNCell(cells)
    else:
        cell = cells[0]

    return cell

def _attention(config, states):
    """ Non-recurrent attention layer that examines all states, for each state, and return a convex mixture of the states. """
   
    # set up
    state_size = states.get_shape().as_list()[2]
    num_states = config['num_steps']
    mlp_size   = config['attention_mlp_size']
    par_iters  = config['num_attention_parallel_iters']
    attn_init  = dict_to_inits(config['attention_init'], config['attention_seed'])
    
    # vars
    kernel = tf.get_variable(name='attention_kernel', shape=[state_size, mlp_size * 2], initializer=attn_init['base'])
    bias   = tf.get_variable(name='attention_bias',   shape=[1, 1, mlp_size],           initializer=attn_init['bias'])
    linear = tf.get_variable(name='attention_linear', shape=[1, 1, mlp_size],           initializer=attn_init['base'])

    # per entry attention function
    def attend(states_single):                                                          # [NUM_STATES, STATE_SIZE]
        combined_conv = tf.matmul(states_single, kernel)                                # [NUM_STATES, 2 x MLP_SIZE]
        query_conv, state_conv = tf.split(combined_conv, 2, 1)                          # 2 x [NUM_STATES, MLP_SIZE]
        all_to_all_sums = tf.expand_dims(query_conv, 1) + tf.expand_dims(state_conv, 0) # [NUM_QUERIES, NUM_STATES, MLP_SIZE]
        energies = tf.reduce_sum(linear * tf.tanh(all_to_all_sums + bias), [2])         # [NUM_QUERIES, NUM_STATES]
        probs = tf.nn.softmax(energies, dim=1)                                          # [NUM_QUERIES, NUM_STATES]
        mixtures = tf.expand_dims(probs, 2) * tf.expand_dims(states_single, 0)          # [NUM_QUERIES, NUM_STATES, STATE_SIZE]    
        mixed = tf.reduce_sum(mixtures, [1])                                            # [NUM_QUERIES, STATE_SIZE]

        return mixed

    # actual computation
    states = tf.transpose(states, [1, 0, 2])                                            # [BATCH_SIZE, NUM_STATES, STATE_SIZE]
    mixes  = tf.map_fn(attend, states, swap_memory=True, parallel_iterations=par_iters) # [BATCH_SIZE, NUM_STATES, STATE_SIZE]
    mixes  = tf.transpose(mixes, [1, 0, 2])                                             # [NUM_STATES, BATCH_SIZE, STATE_SIZE]
    
    return mixes

def _dssps(config, inputs):
    """ Converts internal representation resultant from RNN output activations
        into dssp classes (logits). """

    # create initialization dict
    recurrent_out_proj_init = dict_to_inits(config['recurrent_out_proj_init'], config['recurrent_out_proj_seed'])

    # set up linear transform variables, returning raw logits instead of transforming via softmax so that I can use in loss function
    dssps = layers.fully_connected(inputs, NUM_DSSPS, activation_fn=None, scope='linear_dssps',
                                   weights_initializer=recurrent_out_proj_init['base'], biases_initializer=recurrent_out_proj_init['bias'],
                                   variables_collections={'weights': [tf.GraphKeys.WEIGHTS], 'biases': [tf.GraphKeys.BIASES]}, 
                                   outputs_collections=config['name'] + '_' + tf.GraphKeys.ACTIVATIONS)
            # [NUM_STEPS, BATCH_SIZE, NUM_DSSPS]

    return dssps 

def _secondary_loss(config, dssps, targets, masks, group_filter):
    """ Computes reduced cross entropy loss between predicted secondary 
        structures and targets. """

    # XXX THIS SECTION IS NOT CURRENTLY FUNCTIONAL

    # flatten before cross entropy computation
    my_dssps = tf.identity(dssps, 'my_linear_dssps')

    flattened_dssps   = tf.reshape(dssps,   [-1, NUM_DSSPS]) # [NUM_STEPS x BATCH_SIZE, NUM_DSSPS]
    flattened_targets = tf.reshape(targets, [-1])            # [NUM_STEPS x BATCH_SIZE]

    # compute softmax cross entropy from raw activations, and turn nans into zeroes for predictions past sequence lengths
    flattened_sces = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=flattened_targets, logits=flattened_dssps)
    flattened_sces = tf.where(tf.is_nan(flattened_sces), tf.zeros_like(flattened_sces), flattened_sces)
                     # [NUM_STEPS x BATCH_SIZE] 

    # reshape, reduce, and normalize
    eff_num_stepss = effective_steps(masks, config['num_edge_residues'])                   # [BATCH_SIZE]
    sces = tf.reshape(flattened_sces, [tf.shape(dssps)[0], -1])                            # [NUM_STEPS, BATCH_SIZE] 
    sces_reduced = tf.reduce_sum(sces, 0)                                                  # [BATCH_SIZE]
    sces_normalized = tf.div(sces_reduced, eff_num_stepss, name='secondary_cross_entropy') # [BATCH_SIZE]

    # reduce cross_entropy based on normalization behavior
    loss = _reduce_loss(sces_normalized, masks, group_filter, 
                        config['secondary_normalization'], config['num_edge_residues'], name='secondary_loss')

    # add to relevant collections for summaries, etc.
    if config['log_model_summaries']: 
        tf.add_to_collection(config['name'] + '_scess',            sces_normalized) # never filters by group
        tf.add_to_collection(config['name'] + '_secondary_losses', loss)

    return loss

def _secondary_accuracy(config, dssps, targets, masks, group_filter):
    """ Computes (non-differentiable) accuracy of predicted secondary 
        structures based on targets. """

    # XXX THIS SECTION IS NOT CURRENTLY FUNCTIONAL

    # pick top classes
    tops = tf.to_int32(tf.argmax(dssps, 2)) # [NUM_STEPS, BATCH_SIZE]

    # compute matches, cast, and reduce to counts of matches
    eff_num_stepss = effective_steps(masks, config['num_edge_residues'])         # [BATCH_SIZE]
    matches = tf.to_float(tf.equal(tops, targets))                               # [NUM_STEPS, BATCH_SIZE]
    matches_reduced = tf.reduce_sum(matches, 0)                                  # [BATCH_SIZE]
    matches_normalized = tf.div(matches_reduced, eff_num_stepss, name='matches') # [BATCH_SIZE]

    # reduce accuracy based on normalization behavior
    accuracy = _reduce_loss(matches_normalized, masks, group_filter, 
                            config['secondary_normalization'], config['num_edge_residues'], name='secondary_accuracy')

    # add to relevant collections for summaries, etc.
    if config['log_model_summaries']: 
        tf.add_to_collection(config['name'] + '_matchess',             matches_normalized) # never filters by group
        tf.add_to_collection(config['name'] + '_secondary_accuracies', accuracy)

    return accuracy


def _alphabet(mode, config):
    """ Creates alphabet for alphabetized dihedral prediction. """
    """ Modification: Create alphabet for alphabetized general parameterization prediction (including dihedral, torsion and curvature, quaternions, etc."""


    # prepare initializer
    if config['alphabet'] is not None:
        alphabet_initializer = tf.constant_initializer(config['alphabet']) # user-defined alphabet
    else:
        alphabet_initializer = dict_to_init(config['alphabet_init'], config['alphabet_seed']) # random initialization

    # alphabet variable, possibly trainable
    alphabet = tf.get_variable(name='alphabet',
                               shape=[config['alphabet_size'], config['number_parametrization']], 
                               initializer=alphabet_initializer,
                               trainable=config['alphabet_trainable']) # [OUTPUT_SIZE, NUM_PARAMETERES]
    if mode == 'training' and config['alphabet_trainable']: 
        tf.add_to_collection(tf.GraphKeys.WEIGHTS, alphabet) # add to WEIGHTS collection if trainable

    return alphabet




def _geometric_parametrization(mode, config, inputs, alphabet=None):
    """ Converts internal representation resultant from RNN or transformer output activations
        into geometrical representations based on one of many methods. 

        The optional argument alphabet does not determine whether an alphabet 
        should be created or not--that's controlled by config. Instead the
        option allows the reuse of an existing alphabet. """
    
    is_training = (mode == 'training')

    # output size for linear transform layer (OUTPUT_SIZE)
    output_size = config['alphabet_size'] if config['is_alphabetized'] else config['number_parametrization'] #NUM_DIHEDRALS #Modification: NUM_DIHEDRALS to parametrization
    
    # set up non-linear parameters layer(s) if requested
    nonlinear_out_proj_size = config['recurrent_nonlinear_out_proj_size']
    if nonlinear_out_proj_size is not None:
        if config['recurrent_nonlinear_out_proj_normalization'] == 'batch_normalization':
            nonlinear_out_proj_normalization_fn = layers.batch_norm
            nonlinear_out_proj_normalization_fn_opts = {'center': True, 'scale': True, 'decay': 0.9, 'epsilon': 0.001, 
                                                        'is_training': tf.constant(is_training), 'scope': 'nonlinear_out_proj_batch_norm', 
                                                        'outputs_collections': config['name'] + '_' + tf.GraphKeys.ACTIVATIONS}
        elif config['recurrent_nonlinear_out_proj_normalization'] == 'layer_normalization':
            nonlinear_out_proj_normalization_fn = layers.layer_norm
            nonlinear_out_proj_normalization_fn_opts = {'center': True, 'scale': True, 'scope': 'nonlinear_out_proj_layer_norm', 
                                                        'outputs_collections': config['name'] + '_' + tf.GraphKeys.ACTIVATIONS}
        else:
            nonlinear_out_proj_normalization_fn = None
            nonlinear_out_proj_normalization_fn_opts = None

        nonlinear_out_proj_fn = {'tanh': tf.tanh, 'relu': tf.nn.relu}[config['recurrent_nonlinear_out_proj_function']]

        outputs = inputs
        for idx, (layer_size, init) in enumerate(zip(nonlinear_out_proj_size, config['recurrent_nonlinear_out_proj_init'])):
            recurrent_nonlinear_out_proj_init = dict_to_inits(init, config['recurrent_nonlinear_out_proj_seed'])
            outputs = layers.fully_connected(outputs, layer_size, scope='nonlinear_parameters_' + str(idx), 
                                             activation_fn=nonlinear_out_proj_fn, 
                                             normalizer_fn=nonlinear_out_proj_normalization_fn, 
                                             normalizer_params=nonlinear_out_proj_normalization_fn_opts,
                                             weights_initializer=recurrent_nonlinear_out_proj_init['base'], 
                                             biases_initializer=recurrent_nonlinear_out_proj_init['bias'], 
                                             outputs_collections=config['name'] + '_' + tf.GraphKeys.ACTIVATIONS, 
                                             variables_collections={'weights': [tf.GraphKeys.WEIGHTS], 'biases': [tf.GraphKeys.BIASES]})
        parameter_inputs = outputs 
        # [NUM_STEPS, BATCH_SIZE, NONLINEAR_DIHEDRALS_LAYER_SIZE]
    else:
        parameter_inputs = inputs  
        # [NUM_STEPS, BATCH_SIZE, N x RECURRENT_LAYER_SIZE] where N is 1 or 2 depending on bidirectionality

    # set up linear transform variables
    recurrent_out_proj_init = dict_to_inits(config['recurrent_out_proj_init'], config['recurrent_out_proj_seed'])
    linear = layers.fully_connected(parameter_inputs, output_size, activation_fn=None, scope='linear_parameters',     # Modification dihedral_inputs to parameters_input
                                    weights_initializer=recurrent_out_proj_init['base'], biases_initializer=recurrent_out_proj_init['bias'],
                                    variables_collections={'weights': [tf.GraphKeys.WEIGHTS], 'biases': [tf.GraphKeys.BIASES]}, 
                                    outputs_collections=config['name'] + '_' + tf.GraphKeys.ACTIVATIONS)
             # [NUM_STEPS, BATCH_SIZE, OUTPUT_SIZE]

    # reduce to dihedrals, through an alphabet if specified
    if config['is_alphabetized']:
        # create alphabet if one is not already there
        if alphabet is None: alphabet = _alphabet(mode, config)

        # angularize alphabet if specified
        if config['is_angularized']: alphabet = angularize(alphabet)

        # batch or layer normalize linear inputs to softmax (stats are computed over all batches and timesteps, effectively flattened)
        if config['alphabet_normalization'] == 'batch_normalization':
            linear = layers.batch_norm(linear, center=True, scale=True, decay=0.999, epsilon=0.001, is_training=tf.constant(is_training), 
                                       scope='alphabet_batch_norm', outputs_collections=config['name'] + '_' + tf.GraphKeys.ACTIVATIONS)
        elif config['alphabet_normalization'] == 'layer_normalization':
            linear = layers.layer_norm(linear, center=True, scale=True,
                                       scope='alphabet_layer_norm', outputs_collections=config['name'] + '_' + tf.GraphKeys.ACTIVATIONS)

        # softmax for linear to create parameter mixtures
        flattened_linear = tf.reshape(linear, [-1, output_size])                               # [NUM_STEPS x BATCH_SIZE, OUTPUT_SIZE]
        probs = tf.nn.softmax(flattened_linear / config['alphabet_temperature'], name='probs') # [NUM_STEPS x BATCH_SIZE, OUTPUT_SIZE]      
        tf.add_to_collection(config['name'] + '_' + tf.GraphKeys.ACTIVATIONS, probs)

        # dropout alphabet if specified. I don't renormalize since final angle is invariant wrt overall scale.
        if mode == 'training' and config['alphabet_keep_probability'] < 1:
            probs = tf.nn.dropout(probs, config['alphabet_keep_probability'], seed=config['dropout_seed'], name='dropped_probs')

        # form final parameters based on mixture of alphabetized parameters
        num_steps = tf.shape(linear)[0]
        batch_size = linear.get_shape().as_list()[1]
        flattened_parameters = reduce_mean_angle(probs, alphabet)                            # [NUM_STEPS x BATCH_SIZE, NUM_PARAMETERS]
        parameters = tf.reshape(flattened_parameters, [num_steps, batch_size, config['number_parametrization']]) # [NUM_STEPS, BATCH_SIZE, NUM_PARAMETERS]

    else:
        # just linear
        parameters = linear

        # angularize if specified
        if config['is_angularized']: parameters = angularize(parameters)
    # [NUM_STEPS, BATCH_SIZE, NUM_DIHEDRALS] (for both cases)

    
    #Modification: I think the following is not needed, as angle_shift is adding a vanishing angle
    #parameters = tf.add(parameters, tf.constant(config['angle_shift'], dtype=tf.float32, name='angle_shift'), name='dihedrals')
    #parameters = tf.add(parameters, tf.constant(config['angle_shift'], dtype=tf.float32, name='angle_shift'))

    return parameters

def _coordinates(config, parameters):
    """ Converts dihedrals into full 3D structures. """

    
    # converts dihedrals to points ready for reconstruction.
    #points = dihedral_to_point(parameters) # [NUM_STEPS x NUM_DIHEDRALS, BATCH_SIZE, NUM_DIMENSIONS]
             
    # converts points to final 3D coordinates.
    #coordinates = point_to_coordinate(points, num_fragments=config['num_reconstruction_fragments'], 
    #                                          parallel_iterations=config['num_reconstruction_parallel_iters']) 
                  # [NUM_STEPS x NUM_DIHEDRALS, BATCH_SIZE, NUM_DIMENSIONS]

    #return coordinates


    """ Converts angles into full 3D structures. """

    #converts angles to 3D coordinates.
    
    for case in switch(config['angle_type']):
        if case('dihedrals'):
            points = dihedral_to_point(parameters) # [NUM_STEPS x NUM_ATOM_TYPE, BATCH_SIZE, NUM_DIMENSIONS]
            coordinates = point_to_coordinate(points, num_fragments=config['num_reconstruction_fragments'],
                                                      parallel_iterations=config['num_reconstruction_parallel_iters']) # [NUM_STEPS x ATOM_TYPE, BATCH_SIZE, NUM_DIMENSIONS]
        elif case('torsions_and_curvatures'):
            rotation_translation = torsion_and_curvature_to_rotation_translation(parameters) # [NUM_STEPS, BATCH_SIZE, NUM_DIMENSIONS]
            #coordinates = simple_static_rotation_translation_to_coordinate(rotation_translation, max_num_steps=config['num_steps'])
            coordinates = simple_dynamic_rotation_translation_to_coordinate(rotation_translation, parallel_iterations=config['num_reconstruction_parallel_iters'])
        else:
            raise ValueError ('Angle to final 3D coordinates conversion is not available')
    

    return coordinates

def _drmsds(config, coordinates, targets, weights):
    """ Computes reduced weighted dRMSD loss (as specified by weights) 
        between predicted tertiary structures and targets. """

    # lose end residues if desired
    if config['num_edge_residues'] > 0:
        coordinates = coordinates[:-(config['num_edge_residues'] * config['num_atom_type'])]

    # if only c_alpha atoms are requested then subsample
    # if config['atoms'] == 'c_alpha': # starts at 1 because c_alpha atoms are the second atoms
    #     coordinates = coordinates[1::NUM_DIHEDRALS] # [NUM_STEPS - NUM_EDGE_RESIDUES, BATCH_SIZE, NUM_DIMENSIONS]
    #     targets     =     targets[1::NUM_DIHEDRALS] # [NUM_STEPS - NUM_EDGE_RESIDUES, BATCH_SIZE, NUM_DIMENSIONS]
     
    for case in switch (config['angle_type']):
        if case ('dihedrals'):
            coordinates = coordinates[1::NUM_DIHEDRALS] # [NUM_STEPS - NUM_EDGE_RESIDUES, BATCH_SIZE, NUM_DIMENSIONS]
            targets     =     targets[1::NUM_DIHEDRALS] # [NUM_STEPS - NUM_EDGE_RESIDUES, BATCH_SIZE, NUM_DIMENSIONS]
      
        elif case ('torsions_and_curvatures'):
            #coordinates = tf.identity(coordinates)#[:-(config['num_edge_residues'])]
            #coordinates = coordinates[1:]
            targets     = targets[1::NUM_DIHEDRALS]   
        else:
            raise ValueError ('drmds are not available')

    


    #Modification: NUM_ATOM_TYPE = 3 for C_alpha, C and N or NUM_ATOM_TYPE = 1 for C_alphe

    
    # lose end residues if desired

    # for case in switch (config['angle_type']):
    #     if case ('dihedrals'):
    #         if config['num_edge_residues'] > 0:
    #             coordinates = coordinates[:-(config['num_edge_residues'] * NUM_DIHEDRALS)]

    # # if only c_alpha atoms are requested then subsample
    #         if config['atoms'] == 'c_alpha': # starts at 1 because c_alpha atoms are the second atoms
    #            coordinates = coordinates[1::NUM_DIHEDRALS] # [NUM_STEPS - NUM_EDGE_RESIDUES, BATCH_SIZE, NUM_DIMENSIONS]
    #            targets     =     targets[1::NUM_DIHEDRALS] # [NUM_STEPS - NUM_EDGE_RESIDUES, BATCH_SIZE, NUM_DIMENSIONS]
    #     if case ('torsions_and_curvatures'):
    #         if config['num_edge_residues'] > 0:
    #            coordinates = coordinates[:-(config['num_edge_residues'])]
    #            targets     = targets[1::NUM_DIHEDRALS]              
    # #compute per structure dRMSDs

    drmsds = drmsd(coordinates, targets, weights, name='drmsds') # [BATCH_SIZE]

    # add to relevant collections for summaries, etc.
    if config['log_model_summaries']: tf.add_to_collection(config['name'] + '_drmsdss', drmsds)

    return drmsds

def _reduce_loss_quotient(config, losses, masks, group_filter, name_prefix=''):
    """ Reduces loss according to normalization order. """

    normalization = config['tertiary_normalization']
    num_edge_residues = config['num_edge_residues']
    max_seq_length = config['num_steps']

    losses_filtered = tf.boolean_mask(losses, group_filter) # will give problematic results if all entries are removed

    for case in switch(normalization):
        if case('zeroth'):
            loss_factors = tf.ones_like(losses_filtered)
        elif case ('first'):
            loss_factors = tf.boolean_mask(effective_steps(masks, num_edge_residues), group_filter)
            fixed_denominator_factor = float(max_seq_length - num_edge_residues)
        elif case ('second'):
            eff_num_stepss = tf.boolean_mask(effective_steps(masks, num_edge_residues), group_filter)
            loss_factors = (tf.square(eff_num_stepss) - eff_num_stepss) / 2.0
            fixed_denominator_factor = float(max_seq_length - num_edge_residues)
            fixed_denominator_factor = ((fixed_denominator_factor ** 2) - fixed_denominator_factor) / 2.0

    numerator = tf.reduce_sum(loss_factors * losses_filtered, name=name_prefix + '_numerator')

    if config['batch_dependent_normalization'] or normalization == 'zeroth':
        denominator = tf.reduce_sum(loss_factors, name=name_prefix + '_denominator')
    else:
        denominator = tf.multiply(tf.cast(tf.size(loss_factors), tf.float32), fixed_denominator_factor, name=name_prefix + '_denominator')

    return numerator, denominator

def _accumulate_loss(config, numerator, denominator, name_prefix=''):
    """ Constructs ops to accumulate and reduce loss and maintain a memory of lowest loss achieved """

    if config['num_evaluation_invocations'] == 1:
        # return simple loss
        accumulated_loss = tf.divide(numerator, denominator, name=name_prefix)
        update_op = reduce_op = tf.no_op()
    else:
        # create accumulator variables. note that tf.Variable uses name_scope (not variable_scope) for naming, which is what's desired in this instance
        numerator_accumulator   = tf.Variable(initial_value=0., trainable=False, name=name_prefix + '_numerator_accumulator')
        denominator_accumulator = tf.Variable(initial_value=0., trainable=False, name=name_prefix + '_denominator_accumulator')

        # accumulate
        with tf.control_dependencies([numerator, denominator, numerator_accumulator, denominator_accumulator]):
            accumulate_numerator   = tf.assign_add(numerator_accumulator, numerator)
            accumulate_denominator = tf.assign_add(denominator_accumulator, denominator)
            update_op = tf.group(accumulate_numerator, accumulate_denominator, name=name_prefix + '_accumulate_op')

        # divide to get final quotient
        with tf.control_dependencies([update_op]):
            accumulated_loss = tf.divide(numerator_accumulator, denominator_accumulator, name=name_prefix + '_accumulated')

        # zero accumulators
        with tf.control_dependencies([accumulated_loss]):
            zero_numerator   = tf.assign(numerator_accumulator,   0.)
            zero_denominator = tf.assign(denominator_accumulator, 0.)
            reduce_op = tf.group(zero_numerator, zero_denominator, name=name_prefix + '_reduce_op')

    min_loss_achieved = tf.Variable(initial_value=float('inf'), trainable=False, name='min_' + name_prefix + '_achieved')
    min_loss_op = tf.assign(min_loss_achieved, tf.reduce_min([min_loss_achieved, accumulated_loss]), name='min_' + name_prefix + '_achieved_op')
    with tf.control_dependencies([min_loss_op]):
        min_loss_achieved = tf.identity(min_loss_achieved)

    return accumulated_loss, min_loss_achieved, min_loss_op, update_op, reduce_op

def _training(config, loss):
    """ Creates loss optimizer and returns minimization op. """

    # helper function
    optimizer_args = lambda o: o.__init__.__code__.co_varnames[:o.__init__.__code__.co_argcount]

    # select appropriate optimization function and construct arg list based on config
    optimizer_func = {'steepest': tf.train.GradientDescentOptimizer, # doesn't support momentum, unlike autograd
                      'rmsprop': tf.train.RMSPropOptimizer, 
                      'adam': tf.train.AdamOptimizer, 
                      'momentum': tf.train.MomentumOptimizer,
                      'adagrad': tf.train.AdagradOptimizer,
                      'adadelta': tf.train.AdadeltaOptimizer}[config['optimizer']]
    optimizer_params = config.keys() & set(optimizer_args(optimizer_func))
    optimizer_params_and_values = {param: config[param] for param in optimizer_params}
    optimizer = optimizer_func(**optimizer_params_and_values)

    # obtain and process gradients
    grads_and_vars = optimizer.compute_gradients(loss)
    threshold = config['gradient_threshold']

    if threshold != float('inf'):
        for case in switch(config['rescale_behavior']):
            if case('norm_rescaling'):
                grads, _ = tf.clip_by_global_norm([g for g, _ in grads_and_vars], threshold)
                vars_ = [v for _, v in grads_and_vars]
                grads_and_vars = zip(grads, vars_)
            elif case('hard_clipping'):
                grads_and_vars = [(tf.clip_by_value(g, -threshold, threshold), v) for g, v in grads_and_vars]

    # apply gradients and return stepping op
    global_step = tf.get_variable(initializer=tf.constant_initializer(0), shape=[], trainable=False, dtype=tf.int32, name='global_step')
    minimize_op = optimizer.apply_gradients(grads_and_vars, global_step=global_step)

    # dict useful for diagnostics
    grads_and_vars_dict = {}
    grads_and_vars_dict.update({('g' + str(i)): g for i, (g, _) in enumerate(grads_and_vars)})
    grads_and_vars_dict.update({('v' + str(i)): v for i, (_, v) in enumerate(grads_and_vars)})

    return global_step, minimize_op, grads_and_vars_dict

def _history(config, loss, loss_history=None, scaling_factor=LOSS_SCALING_FACTOR):
    """ Creates op for loss history updating. """

    # op for shifting history, i.e. adding new loss, dropping oldest one
    #new_history = tf.concat([loss_history[1:], tf.expand_dims(loss * scaling_factor, 0)], 0)
    new_history = tf.concat([loss_history[1:], [loss * scaling_factor]], 0)
    with tf.control_dependencies([new_history]):
        update_op = tf.assign(loss_history, new_history, name='update_curriculum_history_op')
                                
    return update_op

def _curriculum(config, step, loss_history, dependency_ops):
    """ Creates TF ops for maintaining and advancing the curriculum. """

    # assign appropriate curriculum increment value
    for case in switch(config['behavior']):
        if case('fixed_rate'):
            # fixed rate, always return same number
            increment = tf.constant(config['rate'], name='curriculum_increment')
        elif case('loss_threshold'):
            # return fixed increment if last loss is below threshold, zero otherwise
            increment_pred = tf.less(loss_history[-1], config['threshold'], name='curriculum_predicate')
            full_increment_func = lambda: tf.constant(config['rate'], name='full_curriculum_increment')
            zero_increment_func = lambda: tf.constant(0.0,            name='zero_curriculum_increment')
            increment = tf.cond(increment_pred, full_increment_func, zero_increment_func)
        elif case('loss_change'):
            # predicate for increment type
            increment_pred = tf.not_equal(loss_history[0], DUMMY_LOSS, name='curriculum_predicate')

            # increment function for when loss history is still
            def full_increment_func():
                lin_seq = tf.expand_dims(tf.linspace(0., 1., config['change_num_iterations']), 1)
                ls_matrix = tf.concat([tf.ones_like(lin_seq), lin_seq], 1)
                ls_rhs = tf.expand_dims(loss_history, 1)
                ls_slope = tf.matrix_solve_ls(ls_matrix, ls_rhs)[1, 0]

                full_increment = tf.div(config['rate'], tf.pow(tf.abs(ls_slope) + 1, config['sharpness']), name='full_curriculum_increment')

                return full_increment

            # dummy increment function for when loss history is changing rapidly
            zero_increment_func = lambda: tf.constant(0.0, name='zero_curriculum_increment')

            # final conditional increment
            increment = tf.cond(increment_pred, full_increment_func, zero_increment_func)

    # create updating op. the semantics are such that training / gradient update is first performed before the curriculum is incremented.
    with tf.control_dependencies(dependency_ops):
        update_op = tf.assign_add(step, increment, name='update_curriculum_op')

    return update_op
