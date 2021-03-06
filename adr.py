import tensorflow as tf
import tensorflow.python.keras.backend as K
from tensorflow.python.keras.layers import Input
from tensorflow.python.keras.layers import RepeatVector
from tensorflow.python.keras.layers import Lambda
from tensorflow.python.keras.layers import BatchNormalization
from tensorflow.python.keras.layers import Activation
from tensorflow.python.keras.layers import Conv2D
from tensorflow.python.keras.layers import ConvLSTM2D
from tensorflow.python.keras.layers import TimeDistributed
from tensorflow.python.keras.models import Model
from tensorflow.python.keras.optimizers import Adam
from tensorflow.python.keras.losses import mean_squared_error
from tensorflow.python.keras.regularizers import l2
from models.encoder_decoder import image_decoder
from models.encoder_decoder import load_decoder
from models.encoder_decoder import recurrent_image_encoder
from models.encoder_decoder import load_recurrent_encoder
from models.encoder_decoder import repeat_skips
from models.encoder_decoder import slice_skips
from models.action_net import action_net
from models.action_net import load_action_net
from models.action_net import load_recurrent_action_net
from models.action_net import recurrent_action_net
from models.lstm import lstm_gaussian
from models.lstm import load_lstm
from models.lstm import lstm_initial_state_zeros



def get_ins(frames, actions, states, use_seq_len=12, gaussian=True, a_units=0, a_layers=0, units=0, layers=0,
            random_window=False, lstm=False):

    initial_state, initial_state_a = None, None
    bs, seq_len = frames.shape[0], frames.shape[1]

    frame_inputs = Input(batch_shape=frames.shape, name='images')
    ins = [frame_inputs]

    if actions is not None:
        action_inputs = Input(batch_shape=actions.shape, name='actions')
        ins.append(action_inputs)
        action_state = action_inputs  # only using actions
    if states is not None:
        state_inputs = Input(batch_shape=states.shape, name='states')
        ins.append(state_inputs)
        action_state = state_inputs  # only using states
    if actions is not None and states is not None:
        action_state = K.concatenate([action_inputs, state_inputs], axis=-1)  # using actions and states

    if random_window:
        rand_index = tf.random.uniform(shape=(), minval=0, maxval=seq_len-use_seq_len+1, dtype='int32')
        frame_slice = tf.slice(frame_inputs, (0, rand_index, 0, 0, 0), (-1, use_seq_len, -1, -1, -1))
        action_state_slice = tf.slice(action_state, (0, rand_index, 0), (-1, use_seq_len, -1))
    else:  # window starts at the start of each sample
        frame_slice = tf.slice(frame_inputs, (0, 0, 0, 0, 0), (-1, use_seq_len, -1, -1, -1))
        action_state_slice = tf.slice(action_state, (0, 0, 0), (-1, use_seq_len, -1))

    if gaussian:
        initial_state_a = lstm_initial_state_zeros(units=a_units, n_layers=a_layers, batch_size=bs)
        ins.append(initial_state_a)
    if lstm:
        initial_state = lstm_initial_state_zeros(units=units, n_layers=layers, batch_size=bs)
        ins.append(initial_state)

    return frame_slice, action_state_slice, initial_state_a, initial_state, ins


def get_sub_model(name, batch_shape, h_dim, ckpt_dir, filename, trainable, load_model_state, load_flag,
                  model_name=None, **kwargs):

    if model_name is None:
        model_name = name

    f_inst = {'Ec': recurrent_image_encoder, 'A': action_net, 'rA': recurrent_action_net,
              'La': lstm_gaussian, 'Da': image_decoder, 'Da2': image_decoder}

    f_load = {'Ec': load_recurrent_encoder, 'A': load_action_net, 'rA': load_recurrent_action_net,
              'La': load_lstm, 'Da': load_decoder, 'Da2': load_decoder}

    f = f_load.get(name) if load_flag else f_inst.get(name)

    model = f(name=model_name, batch_shape=batch_shape, h_dim=h_dim, ckpt_dir=ckpt_dir, filename=filename,
              trainable=trainable, load_model_state=load_model_state, **kwargs)

    return model


def freezeLayer(layer, unfreeze=False):
    """
    e.g.: freezeLayer(E.get_layer(name='Da'))
    """
    if unfreeze is True:
        layer.trainable = True
    else:
        layer.trainable = False
    if hasattr(layer, 'layers'):
        for l in layer.layers:
            freezeLayer(l, unfreeze)


def base_layer(x, filters, kernel_size=5, strides=2, activation='relu', kernel_initializer='he_uniform',
               recurrent=False, convolutional=True, reg_lambda=0.00):

    assert convolutional or recurrent, "At least one of 'convolutional' and 'recurrent' must be True"

    if convolutional is True:
        x = TimeDistributed(Conv2D(filters=filters, kernel_size=kernel_size, strides=strides, padding='same',
                                   kernel_regularizer=l2(reg_lambda), kernel_initializer=kernel_initializer))(x)
    if recurrent is True:
        x = ConvLSTM2D(filters=filters, kernel_size=kernel_size, return_sequences=True, padding='same',
                       activation=None, kernel_regularizer=l2(reg_lambda), kernel_initializer=kernel_initializer)(x)

    bn = BatchNormalization()(x)
    layer_output = Activation(activation)(bn)

    return layer_output


def adr_ao(frames, actions, states, context_frames, Ec, A, D, learning_rate=0.01, gaussian=False, kl_weight=None,
           L=None, use_seq_len=12, lstm_units=None, lstm_layers=None, training=True, reconstruct_random_frame=False,
           random_window=True):

    bs, seq_len, w, h, c = [int(s) for s in frames.shape]
    assert seq_len >= use_seq_len
    frame_inputs, action_state, initial_state, _, ins = get_ins(frames, actions, states, use_seq_len=use_seq_len,
                                                                random_window=random_window, gaussian=gaussian,
                                                                a_units=lstm_units, a_layers=lstm_layers)

    rand_index_1 = tf.random.uniform(shape=(), minval=0, maxval=use_seq_len-context_frames+1, dtype='int32')

    # Random xc_0, as an artificial way of augmenting the dataset
    xc_0 = tf.slice(frame_inputs, (0, rand_index_1, 0, 0, 0), (-1, context_frames, -1, -1, -1))
    xc_1 = tf.slice(frame_inputs, (0, 0, 0, 0, 0), (-1, context_frames, -1, -1, -1))

    x_to_recover = frame_inputs
    n_frames = use_seq_len

    # ===== Build the model
    hc_0, skips_0 = Ec(xc_0)
    hc_1, _ = Ec(xc_1)

    hc_0 = tf.slice(hc_0, (0, context_frames-1, 0), (-1, 1, -1))
    hc_1 = tf.slice(hc_1, (0, context_frames-1, 0), (-1, 1, -1))
    skips = slice_skips(skips_0, start=context_frames-1, length=1)

    if reconstruct_random_frame:
        action_state_len = action_state.shape[-1]
        rand_index_2 = tf.random.uniform(shape=(), minval=0, maxval=use_seq_len, dtype='int32')
        action_state = tf.slice(action_state, (0, 0, 0), (bs, rand_index_2+1, action_state_len))
        x_to_recover = tf.slice(frame_inputs, (0, rand_index_2, 0, 0, 0), (bs, 1, w, h, c))
        n_frames = rand_index_2 + 1
    else:
        skips = repeat_skips(skips, use_seq_len)

    ha = A(action_state)
    hc_repeat = RepeatVector(n_frames)(tf.squeeze(hc_0, axis=1))
    hc_ha = K.concatenate([hc_repeat, ha], axis=-1)

    if gaussian:
        z, mu, logvar, state = L([hc_ha, initial_state])
        z = mu if training is False else z
        hc_ha = K.concatenate([hc_repeat, ha, z], axis=-1)

    if reconstruct_random_frame:
        _, hc_ha = tf.split(hc_ha, [-1, 1], axis=1)
        if gaussian:
            _, mu = tf.split(mu, [-1, 1], axis=1)
            _, logvar = tf.split(logvar, [-1, 1], axis=1)

    x_recovered = D([hc_ha, skips])

    rec_loss = mean_squared_error(x_to_recover, x_recovered)
    sim_loss = mean_squared_error(hc_0, hc_1)

    if gaussian:
        ED = Model(inputs=ins, outputs=[x_recovered, x_to_recover, mu, logvar])
    else:
        ED = Model(inputs=ins, outputs=[x_recovered, x_to_recover])
    ED.add_metric(rec_loss, name='rec_loss', aggregation='mean')
    ED.add_metric(sim_loss, name='sim_loss', aggregation='mean')

    if gaussian:
        kl_loss = kl_unit_normal(mu, logvar)
        ED.add_metric(kl_loss, name='kl_loss', aggregation='mean')
        ED.add_loss(K.mean(rec_loss) + K.mean(sim_loss) + kl_weight * K.mean(kl_loss))
    else:
        ED.add_loss(K.mean(rec_loss) + K.mean(sim_loss))

    ED.compile(optimizer=Adam(lr=learning_rate))

    return ED


def adr(frames, actions, states, context_frames, Ec, Eo, A, Do, Da, La=None, gaussian_a=False, use_seq_len=12,
        lstm_units=256, lstm_layers=1, learning_rate=0.001, random_window=True, reconstruct_random_frame=True):

    bs, seq_len, w, h, c = [int(s) for s in frames.shape]
    assert seq_len > use_seq_len

    frame_inputs, action_state, initial_state, _, ins = get_ins(frames, actions, states, use_seq_len=use_seq_len,
                                                                random_window=random_window, gaussian=gaussian_a,
                                                                a_units=lstm_units, a_layers=lstm_layers)
    # context frames at the beginning
    xc_0 = tf.slice(frame_inputs, (0, 0, 0, 0, 0), (-1, context_frames, -1, -1, -1))
    x_to_recover = frame_inputs
    n_frames = use_seq_len

    # ===== Build the model
    hc_0, skips_0 = Ec(xc_0)
    hc_0 = tf.slice(hc_0, (0, context_frames - 1, 0), (-1, 1, -1))
    skips = slice_skips(skips_0, start=context_frames - 1, length=1)

    if reconstruct_random_frame:
        a_s_dim = action_state.shape[-1]
        rand_index_1 = tf.random.uniform((), minval=0, maxval=use_seq_len, dtype='int32')
        action_state = tf.slice(action_state, (0, 0, 0), (bs, rand_index_1+1, a_s_dim))
        x_to_recover = tf.slice(frames, (0, rand_index_1, 0, 0, 0), (bs, 1, w, h, c))
        n_frames = rand_index_1 + 1
    else:
        skips = repeat_skips(skips, use_seq_len)

    ha = A(action_state)
    hc_repeat = RepeatVector(n_frames)(tf.squeeze(hc_0, axis=1))
    hc_ha = K.concatenate([hc_repeat, ha], axis=-1)

    if gaussian_a:
        _, za, _, _ = La([hc_ha, initial_state])
        hc_ha = K.concatenate([hc_repeat, ha, za], axis=-1)

    if reconstruct_random_frame:
        _, hc_ha = tf.split(hc_ha, [-1, 1], axis=1)
        _, ha = tf.split(ha, [-1, 1], axis=1)
        hc_repeat = hc_0

    x_rec_a = Da([hc_ha, skips])

    # --> Changed the input to Eo from the error image to the full frame and the action only prediction
    x_rec_a_pos = K.relu(x_to_recover - x_rec_a)
    x_rec_a_neg = K.relu(x_rec_a - x_to_recover)

    # xo_rec_a = K.concatenate([x_rec_a_pos, x_rec_a_neg], axis=-1)
    xo_rec_a = K.concatenate([x_to_recover, x_rec_a], axis=-1)

    ho, _ = Eo(xo_rec_a)
    # ho = Eo(xo_rec_a)

    h = K.concatenate([hc_repeat, ha, ho], axis=-1)  # multiple reconstruction

    x_err = Do([h, skips])

    x_err_pos = x_err[:, :, :, :, :3]
    x_err_neg = x_err[:, :, :, :, 3:]
    x_recovered = x_err_pos - x_err_neg
    x_target = x_to_recover - x_rec_a
    x_target_pos = x_rec_a_pos
    x_target_neg = x_rec_a_neg

    # == Autoencoder
    model = Model(inputs=ins, outputs=x_recovered)

    rec_loss = mean_squared_error(x_target, x_recovered)
    model.add_metric(K.mean(rec_loss), name='rec_loss', aggregation='mean')

    rec_loss_pos = mean_squared_error(x_target_pos, x_err_pos)
    model.add_metric(rec_loss_pos, name='rec_loss_pos', aggregation='mean')

    rec_loss_neg = mean_squared_error(x_target_neg, x_err_neg)
    model.add_metric(rec_loss_neg, name='rec_loss_neg', aggregation='mean')

    rec_action_only_loss = mean_squared_error(x_rec_a, x_to_recover)
    model.add_metric(rec_action_only_loss, name='rec_A', aggregation='mean')

    model.add_loss(K.mean(rec_loss) + (K.mean(rec_loss_pos) + K.mean(rec_loss_neg)))

    model.compile(optimizer=Adam(lr=learning_rate))

    return model


def adr_vp_teacher_forcing(frames, actions, states, context_frames, Ec, Eo, A, Do, Da, L, La=None, gaussian_a=False,
                           use_seq_len=12, lstm_a_units=256, lstm_a_layers=1, lstm_units=256, lstm_layers=2,
                           learning_rate=0.001, random_window=False):

    bs, seq_len, w, h, c = [int(s) for s in frames.shape]
    assert seq_len >= use_seq_len

    frame_inputs, action_state, initial_state_a, initial_state, ins = get_ins(frames, actions, states,
                                                                              use_seq_len=use_seq_len,
                                                                              random_window=random_window,
                                                                              gaussian=gaussian_a, a_units=lstm_a_units,
                                                                              a_layers=lstm_a_layers, units=lstm_units,
                                                                              layers=lstm_layers, lstm=True)

    # context frames at the beginning
    xc_0 = tf.slice(frame_inputs, (0, 0, 0, 0, 0), (-1, context_frames, -1, -1, -1))
    n_frames = use_seq_len

    # ===== Build the model
    hc_0, skips_0 = Ec(xc_0)
    hc_0 = tf.slice(hc_0, (0, context_frames - 1, 0), (-1, 1, -1))
    skips_0 = slice_skips(skips_0, start=context_frames - 1, length=1)
    skips = repeat_skips(skips_0, n_frames)

    ha = A(action_state)
    hc_repeat = RepeatVector(n_frames)(tf.squeeze(hc_0, axis=1))
    hc_ha = K.concatenate([hc_repeat, ha], axis=-1)

    if gaussian_a:
        _, za, _, _ = La([hc_ha, initial_state_a])  # za taken as the mean
        hc_ha = K.concatenate([hc_repeat, ha, za], axis=-1)

    x_rec_a = Da([hc_ha, skips])  # agent only prediction

    x_err_pos = K.relu(frame_inputs - x_rec_a)
    x_err_neg = K.relu(x_rec_a - frame_inputs)

    # xo_rec_a = K.concatenate([frame_inputs, x_rec_a], axis=-1)  # -->  Here the action only image is not needed
    xo_rec_a = K.concatenate([x_err_pos, x_err_neg], axis=-1)  # ground truth error components

    remove_first_step = Lambda(lambda _x: tf.split(_x, [1, -1], axis=1))  # new operations
    remove_last_step = Lambda(lambda _x: tf.split(_x, [-1, 1], axis=1))

    ho, _ = Eo(xo_rec_a)

    hc = RepeatVector(n_frames-1)(K.squeeze(hc_0, axis=1))
    skips = repeat_skips(skips_0, ntimes=n_frames-1)

    ha_t, _ = remove_last_step(ha)                              # [0 to 18]
    _, ha_tp1 = remove_first_step(ha)                           # [1 to 19]
    ho_t, _ = remove_last_step(ho)                              # [0 to 18]

    h = tf.concat([hc, ha_t, ha_tp1, ho_t], axis=-1)            # [0 to 18]

    ho_pred, _ = L([h, initial_state])                          # [1 to 19]
    _, ho_tp1 = remove_first_step(ho)                           # [1 to 19] Target for LSTM outputs

    x_rec_a_t, _ = remove_last_step(x_rec_a)                    # [0 to 18] Used to obtain x_curr
    _, x_rec_a_tp1 = remove_first_step(x_rec_a)                 # [1 to 19] Used to obtain x_pred

    _, x_target_pred = remove_first_step(frame_inputs)          #           Target for Do pred reconstruction
    _, x_err_pos_target = remove_first_step(x_err_pos)          #           Target for Do pred reconstruction
    _, x_err_neg_target = remove_first_step(x_err_neg)          #           Target for Do pred reconstruction

    # reconstruct current step
    h = tf.concat([hc, ha_t, ho_t], axis=-1)
    x_err_curr = Do([h, skips])

    x_target_curr, _ = remove_last_step(frame_inputs)           # [0 to 18] Target for x_curr
    x_err_curr_pos = x_err_curr[:, :, :, :, :3]
    x_err_curr_neg = x_err_curr[:, :, :, :, 3:]
    x_curr = x_rec_a_t + x_err_curr_pos - x_err_curr_neg

    # predict one step ahead
    h = tf.concat([hc, ha_tp1, ho_pred], axis=-1)
    x_err_pred = Do([h, skips])

    x_err_pred_pos = x_err_pred[:, :, :, :, :3]
    x_err_pred_neg = x_err_pred[:, :, :, :, 3:]
    x_pred = x_rec_a_tp1 + x_err_pred_pos - x_err_pred_neg

    model = Model(inputs=ins, outputs=[ho_pred, x_curr, x_pred, x_rec_a, x_target_pred], name='vp_model')

    ho_mse = mean_squared_error(y_pred=ho_pred, y_true=ho_tp1)
    model.add_metric(K.mean(ho_mse), name='ho_mse', aggregation='mean')

    rec_curr = mean_squared_error(y_pred=x_curr, y_true=x_target_curr)
    model.add_metric(rec_curr, name='rec_curr', aggregation='mean')

    rec_pred = mean_squared_error(y_pred=x_pred, y_true=x_target_pred)
    model.add_metric(rec_pred, name='rec_pred', aggregation='mean')

    rec_pos = mean_squared_error(y_pred=x_err_pred_pos, y_true=x_err_pos_target)
    rec_neg = mean_squared_error(y_pred=x_err_pred_neg, y_true=x_err_neg_target)

    rec_A = mean_squared_error(y_pred=x_rec_a, y_true=frame_inputs)
    model.add_metric(rec_A, name='rec_A', aggregation='mean')

    # why did I have rec_curr??
    # model.add_loss(0.5*K.mean(ho_mse) + 0.125*K.mean(rec_curr) + 0.125*K.mean(rec_pred)
    #                                   + 0.125*K.mean(rec_pos) + 0.125*K.mean(rec_neg))

    # model.add_loss(0.5*K.mean(ho_mse) + 0.5/3*(K.mean(rec_pred)) + K.mean(rec_pos) + K.mean(rec_neg))
    model.add_loss(K.mean(rec_pred) + K.mean(rec_pos) + K.mean(rec_neg))

    model.compile(Adam(lr=learning_rate))

    return model


def adr_vp_feedback(frames, actions, states, context_frames, Ec, Eo, A, Do, Da, L, La=None, gaussian_a=False,
                    use_seq_len=12, lstm_a_units=256, lstm_a_layers=1, lstm_units=256, lstm_layers=2,
                    learning_rate=0.0, random_window=False):

    bs, seq_len, w, h, c = [int(s) for s in frames.shape]
    assert seq_len >= use_seq_len

    frame_inputs, action_state, initial_state_a, initial_state, ins = get_ins(frames, actions, states,
                                                                              use_seq_len=use_seq_len,
                                                                              random_window=random_window,
                                                                              gaussian=gaussian_a, a_units=lstm_a_units,
                                                                              a_layers=lstm_a_layers, units=lstm_units,
                                                                              layers=lstm_layers, lstm=True)
    # context frames at the beginning
    xc_0 = tf.slice(frame_inputs, (0, 0, 0, 0, 0), (-1, context_frames, -1, -1, -1))
    n_frames = use_seq_len

    # ===== Build the model
    hc_0, skips_0 = Ec(xc_0)
    hc_0 = tf.slice(hc_0, (0, context_frames - 1, 0), (-1, 1, -1))
    skips_0 = slice_skips(skips_0, start=context_frames - 1, length=1)
    skips = repeat_skips(skips_0, n_frames)

    ha = A(action_state)
    hc_repeat = RepeatVector(n_frames)(tf.squeeze(hc_0, axis=1))
    hc_ha = K.concatenate([hc_repeat, ha], axis=-1)

    if gaussian_a:
        _, za, _, _ = La([hc_ha, initial_state_a])  # za taken as the mean
        hc_ha = K.concatenate([hc_repeat, ha, za], axis=-1)

    x_rec_a = Da([hc_ha, skips])  # agent only prediction

    x_err_pos = K.relu(frame_inputs - x_rec_a)
    x_err_neg = K.relu(x_rec_a - frame_inputs)
    xo_rec_a = K.concatenate([x_err_pos, x_err_neg], axis=-1)  # ground truth error components

    ho, _ = Eo(xo_rec_a)

    h_pred = []
    prev_state = initial_state
    hc_t = hc_0
    ha_t, _ = tf.split(ha, [-1, 1], axis=1)  # remove last step
    _, ha_tp1 = tf.split(ha, [1, -1], axis=1)  # remove first step

    for i in range(n_frames - 1):

        ho_t, ho = tf.split(ho, [1, -1], axis=1)

        if i >= context_frames:
            ho_t = ho_pred  # hallucinate

        _ha_t, ha_t = tf.split(ha_t, [1, -1], axis=1)
        _ha_tp1, ha_tp1 = tf.split(ha_tp1, [1, -1], axis=1)

        h = tf.concat([hc_t, _ha_t, _ha_tp1, ho_t], axis=-1)

        ho_pred, state = L([h, prev_state])

        h_pred_t = tf.concat([hc_t, _ha_tp1, ho_pred], axis=-1)
        h_pred.append(h_pred_t)
        prev_state = state

    # Obtain predicted frames
    h_pred = tf.squeeze(tf.stack(h_pred, axis=1), axis=2)
    skips = repeat_skips(skips_0, ntimes=n_frames - 1)
    _, xa = tf.split(x_rec_a, [1, -1], axis=1)
    x_err_pred = Do([h_pred, skips])
    x_err_pred_pos = x_err_pred[:, :, :, :, :3]
    x_err_pred_neg = x_err_pred[:, :, :, :, 3:]
    x_pred = xa + x_err_pred_pos - x_err_pred_neg
    _, x_target = tf.split(frame_inputs, [1, -1], axis=1)

    outs = [x_pred, x_pred, x_pred, x_rec_a, x_target]  # repetitions to match teacher forcing version

    model = Model(inputs=ins, outputs=outs, name='vp_model')

    rec_pred = mean_squared_error(y_pred=x_pred, y_true=x_target)
    model.add_metric(rec_pred, name='rec_pred', aggregation='mean')

    rec_A = mean_squared_error(y_pred=x_rec_a, y_true=frame_inputs)
    model.add_metric(rec_A, name='rec_A', aggregation='mean')

    model.add_loss(K.mean(rec_pred))

    model.compile(optimizer=Adam(lr=learning_rate))

    return model


def adr_vp_feedback_frames(frames, actions, states, context_frames, Ec, Eo, A, Do, Da, L, La=None, gaussian_a=False,
                           use_seq_len=12, lstm_a_units=256, lstm_a_layers=1, lstm_units=256, lstm_layers=2,
                           learning_rate=0.0, random_window=False):

    bs, seq_len, w, h, c = [int(s) for s in frames.shape]
    assert seq_len >= use_seq_len

    frame_inputs, action_state, initial_state_a, initial_state, ins = get_ins(frames, actions, states,
                                                                              use_seq_len=use_seq_len,
                                                                              random_window=random_window,
                                                                              gaussian=gaussian_a, a_units=lstm_a_units,
                                                                              a_layers=lstm_a_layers, units=lstm_units,
                                                                              layers=lstm_layers, lstm=True)
    # context frames at the beginning
    xc_0 = tf.slice(frame_inputs, (0, 0, 0, 0, 0), (-1, context_frames, -1, -1, -1))
    n_frames = use_seq_len

    # ===== Build the model
    hc_0, skips_0 = Ec(xc_0)
    hc_0 = tf.slice(hc_0, (0, context_frames - 1, 0), (-1, 1, -1))
    skips_0 = slice_skips(skips_0, start=context_frames - 1, length=1)
    skips = repeat_skips(skips_0, n_frames)

    ha = A(action_state)
    hc_repeat = RepeatVector(n_frames)(tf.squeeze(hc_0, axis=1))
    hc_ha = K.concatenate([hc_repeat, ha], axis=-1)

    if gaussian_a:
        _, za, _, _ = La([hc_ha, initial_state_a])  # za taken as the mean
        hc_ha = K.concatenate([hc_repeat, ha, za], axis=-1)

    x_rec_a = Da([hc_ha, skips])  # agent only prediction

    # x_err_pos = K.relu(frame_inputs - x_rec_a)
    # x_err_neg = K.relu(x_rec_a - frame_inputs)
    # xo_rec_a = K.concatenate([x_err_pos, x_err_neg], axis=-1)  # ground truth error components

    # ho, _ = Eo(xo_rec_a)

    x_pred = []
    prev_state = initial_state
    hc_t = hc_0

    ha_t, _ = tf.split(ha, [-1, 1], axis=1)  # remove last step
    _, ha_tp1 = tf.split(ha, [1, -1], axis=1)  # remove first step
    _, xa_tp1 = tf.split(x_rec_a, [1, -1], axis=1)
    x = frame_inputs
    xa = x_rec_a

    for i in range(n_frames - 1):

        xa_t, xa = tf.split(xa, [1, -1], axis=1)
        xa_pred, xa_tp1 = tf.split(xa_tp1, [1, -1], axis=1)
        x_t, x = tf.split(x, [1, -1], axis=1)

        if i >= context_frames:
            x_t = x_pred_t

        x_xa_t = K.concatenate([x_t, xa_t], axis=-1)
        ho_t, _ = Eo(x_xa_t)

        _ha_t, ha_t = tf.split(ha_t, [1, -1], axis=1)
        _ha_tp1, ha_tp1 = tf.split(ha_tp1, [1, -1], axis=1)

        h = tf.concat([hc_t, _ha_t, _ha_tp1, ho_t], axis=-1)

        ho_pred, state = L([h, prev_state])

        h_pred_t = tf.concat([hc_t, _ha_tp1, ho_pred], axis=-1)

        x_err_pred_t = Do([h_pred_t, skips_0])
        x_err_pred_pos = x_err_pred_t[:, :, :, :, :3]
        x_err_pred_neg = x_err_pred_t[:, :, :, :, 3:]
        x_pred_t = xa_pred + x_err_pred_pos - x_err_pred_neg
        x_pred.append(x_pred_t)

        prev_state = state

    # Obtain predicted frames
    x_pred = tf.squeeze(tf.stack(x_pred, axis=1), axis=2)
    _, x_target = tf.split(frame_inputs, [1, -1], axis=1)

    outs = [x_pred, x_pred, x_pred, x_rec_a, x_target]  # repetitions to match teacher forcing version

    model = Model(inputs=ins, outputs=outs, name='vp_model')

    rec_pred = mean_squared_error(y_pred=x_pred, y_true=x_target)
    model.add_metric(rec_pred, name='rec_pred', aggregation='mean')

    rec_A = mean_squared_error(y_pred=x_rec_a, y_true=frame_inputs)
    model.add_metric(rec_A, name='rec_A', aggregation='mean')

    model.add_loss(K.mean(rec_pred))

    model.compile(optimizer=Adam(lr=learning_rate))

    return model


def kl_unit_normal(_mean, _logvar):
    # KL divergence has a closed form solution for unit gaussian
    # See: https://stats.stackexchange.com/questions/318184/kl-loss-with-a-unit-gaussian
    _kl_loss = - 0.5 * K.sum(1.0 + _logvar - K.square(_mean) - K.exp(_logvar), axis=[-1, -2])
    return _kl_loss

