#!/usr/bin/env python3

from __future__ import generator_stop

import time

import tensorflow as tf
import numpy as np
import sys
import scipy.io.wavfile

from config import *

import generate_wav_samples as gen

def cw_model(features, labels=None, mode=tf.estimator.ModeKeys.PREDICT, params={}):

    p_max_timesteps         = params.get('max_timesteps')
    p_batch_size            = params.get('batch_size')
    p_num_features          = params.get('num_features')
    p_input_layer_depth     = params.get('input_layer_depth')
    p_input_layer_width     = params.get('input_layer_width')
    p_recurrent_layer_depth = params.get('recurrent_layer_depth')
    p_recurrent_layer_width = params.get('recurrent_layer_width')
    p_output_layer_depth    = params.get('output_layer_depth')
    p_output_layer_width    = params.get('output_layer_width')

    is_training = mode == tf.estimator.ModeKeys.TRAIN

    if p_max_timesteps == None:
        p_max_timesteps

    if p_batch_size == None:
        pass

    ####################################################################
    # INPUT
    #
    # -VVV- [p_max_timesteps, p_batch_size, p_num_features]

    # Has size p_max_timesteps, [p_batch_size, p_num_features].
    # Note chat p_num_features is the size of the audio data chunk processed
    # at each step, which is the number of input features.
    seq_len=tf.constant(p_max_timesteps, dtype=tf.int32, shape=[p_batch_size])

    I = features

    ####################################################################
    # INPUT DENSE BAND
    #
    # -^^^- [p_max_timesteps, p_batch_size, p_num_features]
    I = tf.reshape(I, [p_max_timesteps * p_batch_size, p_num_features])
    # -VVV- [p_max_timesteps * p_batch_size, p_num_features]

    for i in range(p_input_layer_depth):
        I = tf.layers.dense(
            I,
            p_input_layer_width,
            kernel_initializer = tf.orthogonal_initializer(1.0),
            bias_initializer = tf.zeros_initializer(),
            activation=None,
            name="inputDense%d" % i
        )
        #I = tf.contrib.layers.batch_norm(I, is_training=is_training)
        I = tf.nn.relu(I)
        I = tf.layers.dropout(
            inputs=I,
            rate=0.0,
            training=is_training
        )


    ####################################################################
    # RECURRENT BAND
    #
    # -^^^- [p_max_timesteps * p_batch_size, p_input_layer_width]
    I = tf.reshape(I, [p_max_timesteps, p_batch_size, p_input_layer_width])
    # -VVV- [p_max_timesteps, p_batch_size, p_input_layer_width]

    cells = []
    with tf.variable_scope("", initializer=tf.orthogonal_initializer(1.0)):
        for i in range(p_recurrent_layer_depth):
            cells.append(tf.contrib.rnn.LayerNormBasicLSTMCell(
                p_recurrent_layer_width,
                forget_bias=1.0,
                activation=tf.nn.relu,
                layer_norm=True,
                norm_gain=1.0,
                norm_shift=0.0,
                dropout_keep_prob=0.5 if is_training else 1.0
            ))
    stack = tf.contrib.rnn.MultiRNNCell(cells)
    I, _ = tf.nn.dynamic_rnn(
        stack,
        I,
        sequence_length=seq_len,
        dtype=tf.float32,
        time_major=True
    )

    ####################################################################
    # OUTPUT DENSE BAND
    #
    # -^^^- [p_max_timesteps, p_batch_size, p_recurrent_layer_width]
    I = tf.reshape(I, [p_max_timesteps * p_batch_size, p_recurrent_layer_width])
    # -VVV- [p_max_timesteps * p_batch_size, p_recurrent_layer_width]

    for i in range(p_output_layer_depth):
        # The last layer must be NUM_CLASSES wide, previous layers can be set arbitrarily
        _width = NUM_CLASSES if i == p_output_layer_depth - 1 else p_output_layer_width
        I = tf.layers.dense(
            I,
            _width,
            kernel_initializer = tf.orthogonal_initializer(1.0),
            bias_initializer = tf.zeros_initializer(),
            activation=None,
            name="outputDense%d" % i
        )
        #I = tf.contrib.layers.batch_norm(I, is_training=is_training)
        I = tf.nn.relu(I)
        I = tf.layers.dropout(
            inputs=I,
            rate=0.0,
            training=is_training
        )

    ####################################################################
    # OUTPUT
    #
    # -^^^- [p_max_timesteps * p_batch_size, NUM_CLASSES]
    I = tf.reshape(I, [p_max_timesteps, p_batch_size, NUM_CLASSES])
    # -VVV- [p_max_timesteps, p_batch_size, NUM_CLASSES]

    if is_training:
        decoded, log_prob = tf.nn.ctc_greedy_decoder(I, seq_len)
    else:
        #decoded, log_prob = tf.nn.ctc_beam_search_decoder(I, seq_len, beam_width=100)
        decoded, log_prob = tf.nn.ctc_greedy_decoder(I, seq_len)

    if mode == tf.estimator.ModeKeys.PREDICT:
        predictions = {
            'decoded': tf.sparse_tensor_to_dense(decoded[0]),
            'log_prob': log_prob
        }
        return tf.estimator.EstimatorSpec(mode, predictions=predictions)

    # ctc_loss is by default time major
    ctc_loss = tf.reduce_mean(tf.nn.ctc_loss(labels, I, seq_len))
    tf.summary.scalar('ctc_loss', ctc_loss)

    # L2 Regularization
    lambda_l2_reg = 0.005
    reg_loss = lambda_l2_reg * tf.reduce_sum([ tf.nn.l2_loss(tf_var) for tf_var in tf.trainable_variables() if not ("bias" in tf_var.name) ])
    tf.summary.scalar('reg_loss', reg_loss)

    loss = ctc_loss + reg_loss
    tf.summary.scalar('loss', loss)

    optimizer = tf.train.AdamOptimizer()
    gvs = optimizer.compute_gradients(loss)
    capped_gvs = [(tf.clip_by_value(grad, -1., 1.), var) for grad, var in gvs]

    ler = tf.reduce_mean(
        tf.edit_distance(tf.cast(decoded[0], tf.int32), labels)
    )

    metrics = {
        'ler': (ler, tf.no_op())
    }

    tf.summary.scalar('ler', ler)

    if mode == tf.estimator.ModeKeys.EVAL:
        return tf.estimator.EstimatorSpec(mode, loss=loss, eval_metric_ops=metrics)

    assert mode == tf.estimator.ModeKeys.TRAIN

    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    with tf.control_dependencies(update_ops):
        train_op = optimizer.apply_gradients(capped_gvs, tf.train.get_global_step())    # Inaccuracy: label error rate

    return tf.estimator.EstimatorSpec(
        mode,
        loss=loss,
        train_op=train_op,
        scaffold=tf.train.Scaffold(
            saver=tf.train.Saver(
                max_to_keep=30
            )
        )
    )

if __name__ == "__main__":

    def main(args):

        batch_size = 250
        num_batches_per_epoch = 60

        estimator = tf.estimator.Estimator(
            model_fn=cw_model,
            model_dir='./model_train',
            params={
                'max_timesteps': TIMESTEPS,
                'batch_size': batch_size,
                'num_features': CHUNK,
                'input_layer_depth': 0,
                'input_layer_width': CHUNK,
                'recurrent_layer_depth': 3,
                'recurrent_layer_width': 128,
                'output_layer_depth': 1,
                'output_layer_width': 128
            }
        )

        def input_fn(params={}):
            return tf.data.Dataset.from_generator(
                lambda: gen.seq_generator(SEQ_LENGTH, FRAMERATE, CHUNK),
                (tf.float32, tf.int64, tf.int32, tf.int64)
            ).apply(tf.contrib.data.map_and_batch(
                lambda a, i, v, s: (a,tf.SparseTensor(i,v,s)),
                batch_size, # BATCH SIZE
                num_parallel_batches=2
            )).map(
                lambda a, l: (tf.transpose(a, (1,0,2)), l), # Switch to time major
                num_parallel_calls=2
            ).take(
                num_batches_per_epoch  # NUMBER OF BATCHES PER EPOCH
            ).prefetch(
                3
            )

        train_spec = tf.estimator.TrainSpec(
            input_fn=input_fn,
            max_steps=100000
        )

        eval_spec = tf.estimator.EvalSpec(
            input_fn=input_fn,
            steps=5,
            throttle_secs=1800,
            start_delay_secs=1800,
        )

        tf.estimator.train_and_evaluate(
            estimator,
            train_spec,
            eval_spec
        )

    tf.logging.set_verbosity(tf.logging.INFO)
    tf.app.run(main)
