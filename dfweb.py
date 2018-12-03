import json
import os
import pandas as pd
from configparser import NoSectionError
from forms.deploy_form import DeploymentForm
from forms.parameters_form import GeneralParamForm
from forms.upload_user import UploadUserForm
from utils import run_utils, upload_util, db_ops, param_utils, config_ops, sys_ops
from config import config_reader
from database.db import db
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_file
from flask_bootstrap import Bootstrap
from flask_login import LoginManager, login_user, login_required, logout_user
from forms.login_form import LoginForm
from forms.register import RegisterForm
from forms.upload_form import UploadForm

from user import User
from utils import explain_util
from thread_handler import ThreadHandler
from session import Session
from utils.request_util import *
from utils.visualize_util import get_norm_corr
from functools import wraps
from generator.simulator import parse
from utils.metrics import *
from utils import custom

SAMPLE_DATA_SIZE = 5
WTF_CSRF_SECRET_KEY = os.urandom(42)

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

Bootstrap(app)
app.secret_key = WTF_CSRF_SECRET_KEY
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///username.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JSON_SORT_KEYS'] = False

th = ThreadHandler()
login_manager = LoginManager()
login_manager.init_app(app)


def check_config(func):
    @wraps(func)
    def check_session(*args, **kwargs):
        if 'token' in request.form and 'token' in session:
            if session['token'] != request.form['token']:
                return redirect(url_for('login'))
        return func(*args, **kwargs)

    return check_session


@login_manager.user_loader
def load_user(user_id):
    return User.query.filter_by(id=user_id).first()


@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        if not db_ops.checklogin(form, login_user, session, sess):
            return render_template('login.html', form=form, error='Invalid username or password')
        return redirect(url_for('upload'))
    return render_template('login.html', form=form)


@app.route('/signup', methods=['GET', 'POST'])
@login_required
def signup():
    form = RegisterForm()
    if form.validate_on_submit():
        if form.password.data != form.password2.data:
            return render_template('signup.html', form=form, error="Passwords are not equals", user=session['user'])
        if not db_ops.sign_up(form):
            return render_template('signup.html', form=form, error="Username already exists", user=session['user'])
        return render_template('login.html', form=LoginForm())
    return render_template('signup.html', form=form, user=session['user'])


@app.route('/user_data', methods=['GET', 'POST'])
@login_required
def user_data():
    username = session['user']
    form = UploadUserForm()
    if form.validate_on_submit():
        email = form.email.data
        db_ops.update_user(username, email)
    db_ops.get_user_data(username, form)
    _, param_configs = config_ops.get_configs_files(APP_ROOT, username)
    user_dataset = config_ops.get_datasets(APP_ROOT, username)
    return render_template('upload_user.html', form=form, user=session['user'], token=session['token'],
                           datasets=user_dataset, parameters=param_configs)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    sess.reset_user()  # TODO new session??
    username = session['user']
    form = UploadForm()
    if form.validate_on_submit():
        if request.form['selected'] == 'tabular_data':
            if not form.new_tabular_files.data['train_file'] == '':
                config_ops.new_config(form.new_tabular_files.data['train_file'],
                                      form.new_tabular_files.data['test_file'], APP_ROOT, username)
        elif request.form['selected'] == 'images':
            option_selected = form.selector.data['selector']
            file = form[option_selected].data['file']
            if not config_ops.new_image_dataset(APP_ROOT, username, option_selected, file):
                return 'Error'
        return 'Ok'

    examples = upload_util.get_examples()
    return render_template('upload.html', title='Data upload', form=form, page=0, examples=examples, user=username,
                           user_configs=config_ops.get_datasets(APP_ROOT, username), token=session['token'])


@app.route('/gui', methods=['GET', 'POST'])
@login_required
@check_config
def gui():
    username = session['user']

    _, param_configs = config_ops.get_configs_files(APP_ROOT, username)
    user_dataset = config_ops.get_datasets_and_types(APP_ROOT, username)

    if not sess.check_key('config_file'):
        return render_template('gui.html', user=username, token=session['token'], page=1, user_dataset=user_dataset,
                               dataset_params={}, data=None, parameters=param_configs, cy_model=[],
                               model_name='new_model', num_outputs=None)
    hlp = sess.get_helper()
    return render_template('gui.html', token=session['token'], page=1, user=username, user_dataset=user_dataset,
                           parameters=param_configs, cy_model=sys_ops.load_cy_model(sess.get_model_name(), username),
                           model_name=sess.get_model_name(), num_outputs=hlp.get_num_outputs(),
                           dataset_params=hlp.get_dataset_params(), data=hlp.get_data())


@app.route('/gui_load', methods=['POST'])
@login_required
@check_config
def gui_load():
    username = session['user']
    _, param_configs = config_ops.get_configs_files(APP_ROOT, username)
    user_dataset = config_ops.get_datasets_and_types(APP_ROOT, username)

    model_name = request.form['model']
    sess.set_config_file(sys_ops.get_config_path(APP_ROOT, username, model_name))

    if sess.load_config():
        hlp = sess.get_helper()
        return render_template('gui.html', token=session['token'], page=1, user=username, user_dataset=user_dataset,
                               parameters=param_configs, dataset_params=hlp.get_dataset_params(), data=hlp.get_data(),
                               cy_model=sys_ops.load_cy_model(model_name, username),
                               model_name=model_name, num_outputs=hlp.get_num_outputs())
    #  TODO send error config not loaded
    return render_template('gui.html', token=session['token'], page=1, user=username, user_dataset=user_dataset,
                           dataset_params={}, data=None, parameters=param_configs, cy_model=[], model_name='new_model',
                           num_outputs=None)


@app.route('/gui_select_data', methods=['POST'])
@login_required
@check_config
def gui_select_data():
    sess.reset_user()
    dataset_name, user = get_dataset(request), session['user']
    upload_util.new_config(dataset_name, user, sess, APP_ROOT)
    return jsonify(data=False)


@app.route('/gui_split', methods=['POST'])
@login_required
@check_config
def gui_split():
    hlp = sess.get_helper()
    hlp.set_split(get_split(request))
    return jsonify(data=hlp.get_data())


@app.route('/gui_features', methods=['POST'])
@login_required
@check_config
def gui_features():
    data = sess.get_helper().process_features_request(request)
    return jsonify(data=data)


@app.route('/gui_targets', methods=['POST'])
@login_required
@check_config
def gui_targets():
    result = sess.get_helper().process_targets_request(request)
    return jsonify(**result)


@app.route('/gui_editor', methods=['GET', 'POST'])
@login_required
@check_config
def gui_editor():
    sess.set_custom(request.get_json())
    return jsonify(explanation='ok')


@app.route('/save_canned', methods=['GET', 'POST'])
@login_required
@check_config
def save_canned():  # TODO not allowed if image (for now)
    # Load cy custom model
    custom_path = sys_ops.create_custom_path(APP_ROOT, session['user'], get_model_name(request))
    cy_model = get_cy_model(request)
    custom.save_cy_model(custom_path, cy_model)

    data = get_data(request)
    data['loss_function'] = get_loss(request)

    sess.set_canned_data(data)
    sess.set_cy_model(cy_model)
    return jsonify(explanation='ok')


@app.route('/save_model', methods=['GET', 'POST'])
@login_required
@check_config
def save_model():
    model_name = request.values['modelname']
    sess.set_model_name(model_name)
    sess.set_mode('canned')
    if request.values['mode'] == '0':
        sess.set_mode('custom')
        path = sys_ops.get_models_pat(APP_ROOT, session['user'])
        os.makedirs(path, exist_ok=True)
        c_path, t_path = custom.save_model_config(sess.get_model(), path, sess.get_cy_model(), model_name)
        sess.set_custom_path(c_path)
        sess.set_transform_path(t_path)
    return redirect(url_for('parameters'))


@app.route('/parameters', methods=['GET', 'POST'])
@login_required
@check_config
def parameters():
    form = GeneralParamForm()
    if form.validate_on_submit():
        sess.get_writer().populate_config(request.form)
        return redirect(url_for('run'))
    username = session['user']
    config_path = sys_ops.get_config_path(APP_ROOT, username, sess.get_model_name())
    sess.set_config_file(config_path)
    param_utils.set_form(form, sess.get_config_file())
    sess.write_config()
    return render_template('parameters.html', title="Parameters", form=form, page=2, user=username,
                           token=session['token'])


@app.route('/run', methods=['GET', 'POST'])
@login_required
@check_config
def run():
    username = session['user']
    config_ops.define_new_model(APP_ROOT, username, sess.get_writer(), sess.get_model_name())
    sess.write_params()
    all_params_config = config_reader.read_config(sess.get_config_file())
    if sess.mode_is_canned():
        all_params_config.set_canned_data(sess.get_canned_data())
    all_params_config.set_email(db_ops.get_email(username))
    export_dir = all_params_config.export_dir()

    checkpoints = run_utils.get_eval_results(export_dir, sess.get_writer(), sess.get_config_file())
    th.run_tensor_board(username, sess.get_config_file())
    running = th.check_running(username)
    sess.run_or_pause(running)

    if request.method == 'POST':
        sess.run_or_pause(is_run(request))
        sess.check_log_fp(all_params_config)
        th.handle_request(get_action(request), all_params_config, username, get_resume_from(request))
        return jsonify(True)

    params, explain = sess.get_helper().get_default_data_example()
    return render_template('run.html', title="Run", page=3, checkpoints=checkpoints, user=username,
                           token=session['token'], port=th.get_port(username, sess.get_config_file()),
                           running=sess.get_status(), metric=sess.get_metric(), params=params, hh=explain)


@app.route('/predict', methods=['POST'])
@login_required
@check_config
def predict():
    hlp = sess.get_helper()
    all_params_config = run_utils.create_result_parameters(request, sess)
    new_features = hlp.get_new_features(request.form, default_features=False)
    if sess.mode_is_canned():
        all_params_config.set_canned_data(sess.get_canned_data())
    final_pred = th.predict_estimator(all_params_config, new_features)
    return jsonify(error=True) if final_pred is None else jsonify(
        run_utils.get_predictions(hlp.get_targets(), final_pred))


@app.route('/explain', methods=['POST', 'GET'])
@login_required
@check_config
def explain():
    if request.method == 'POST':
        hlp = sess.get_helper()
        all_params_config = run_utils.create_result_parameters(request, sess)
        ep = hlp.process_explain_request(request)
        if 'explanation' in ep:
            return jsonify(**ep)
        if sess.mode_is_canned():
            all_params_config.set_canned_data(sess.get_canned_data())
        result = th.explain_estimator(all_params_config, ep)
        return jsonify(explanation=explain_util.explain_return(sess, hlp.get_new_features(request.form), result,
                                                               hlp.get_targets()))
    else:
        return render_template('explain.html', title="Explain", page=5, graphs=sess.get_dict_graphs(),
                               predict_table=sess.get_dict_table(), features=sess.get_new_features(),
                               model=sess.get_model(), exp_target=sess.get_exp_target(), type=sess.get_type(),
                               user=session['user'], token=session['token'])


@app.route('/test', methods=['POST', 'GET'])
@login_required
@check_config
def test():
    hlp = sess.get_helper()
    has_targets, test_filename, df_test, result = hlp.test_request(request)
    if not has_targets:
        return jsonify(result=result)

    all_params_config = config_reader.read_config(sess.get_config_file())
    all_params_config.set('PATHS', 'checkpoint_dir', os.path.join(all_params_config.export_dir(), get_model(request)))

    if sess.mode_is_canned():
        all_params_config.set_canned_data(sess.get_canned_data())

    final_pred = th.predict_test_estimator(all_params_config, test_filename)
    if final_pred is None:
        return jsonify(result='Model\'s structure does not match the new parameter configuration')

    predict_file = hlp.process_test_predict(df_test, final_pred, test_filename)
    sess.set_has_targets(has_targets)
    sess.set_predict_file(predict_file)
    store_predictions(has_targets, sess, final_pred, df_test[hlp.get_targets()].values)
    return jsonify(result="ok")


@app.route('/data_graphs', methods=['POST', 'GET'])
@login_required
@check_config
def data_graphs():
    if request.method == 'POST':
        sess.set_generate_df(get_generate_dataset_name(request), APP_ROOT)
        return jsonify(explanation='ok')
    else:
        df_as_json, norm, corr = get_norm_corr(sess.get('generated_df').copy())
        return render_template('data_graphs.html', data=json.loads(df_as_json), norm=norm, corr=corr)


@app.route('/delete', methods=['POST'])
@login_required
@check_config
def delete():
    all_params_config = config_reader.read_config(sess.get_config_file())
    export_dir = all_params_config.export_dir()
    del_id = get_delete_id(request)
    paths = [del_id] if del_id != 'all' else [d for d in os.listdir(export_dir) if
                                              os.path.isdir(os.path.join(export_dir, d))]
    sys_ops.delete_recursive(paths, export_dir)
    checkpoints = run_utils.get_eval_results(export_dir, sess.get_writer(), sess.get_config_file())
    return jsonify(checkpoints=checkpoints)


@app.route('/delete_model', methods=['POST'])
@login_required
@check_config
def delete_model():
    username = session['user']
    sys_ops.delete_models(get_all(request), [get_model(request)], username)
    _, models = config_ops.get_configs_files(APP_ROOT, username)
    datasets = config_ops.get_datasets(APP_ROOT, username)
    return jsonify(datasets=datasets, models=models)


@app.route('/delete_dataset', methods=['POST'])
@login_required
@check_config
def delete_dataset():
    username = session['user']
    sys_ops.delete_dataset(get_all(request), get_dataset(request), get_models(request), username)
    _, models = config_ops.get_configs_files(APP_ROOT, username)
    datasets = config_ops.get_datasets(APP_ROOT, username)
    return jsonify(datasets=datasets, models=models)


@app.route('/refresh', methods=['GET'])
@login_required
@check_config
def refresh():
    all_params_config = config_reader.read_config(sess.get_config_file())
    running = th.check_running(session['user'])
    sess.run_or_pause(running)
    hlp = sess.get_helper()
    epochs = run_utils.get_step(hlp.get_train_size(), all_params_config.train_batch_size(),
                                all_params_config.checkpoint_dir())
    try:
        config_file = sess.get_config_file()
        export_dir = config_reader.read_config(config_file).export_dir()
        checkpoints = run_utils.get_eval_results(export_dir, sess.get_writer(), config_file)
        return jsonify(checkpoints=checkpoints, data=sess.get('log_fp').read(), running=running, epochs=epochs)
    except (KeyError, NoSectionError):
        return jsonify(checkpoints='', data='', running=running, epochs=epochs)


@app.route('/confirm', methods=['GET', 'POST'])
@login_required
@check_config
def confirm():
    dataset_name = get_datasetname(request)
    script = get_script(request)
    main_path = sys_ops.get_dataset_path(APP_ROOT, session['user'], dataset_name)
    e = parse(script, main_path, dataset_name)
    if e is not True:
        e = str(e).split("Expecting: ")[0]
    sys_ops.create_split_folders(main_path)
    return jsonify(valid=str(e))


@app.route("/download", methods=['GET', 'POST'])
def download():
    file_path = sess.get_predict_file()
    filename = file_path.split('/')[-1]
    return send_file(file_path, mimetype='text/csv', attachment_filename=filename, as_attachment=True)


@app.route("/show_test", methods=['GET', 'POST'])
def show_test():
    hlp = sess.get_helper()
    file_path = sess.get_predict_file()
    df = pd.read_csv(file_path)
    predict_table = {'data': df.as_matrix().tolist(),
                     'columns': [{'title': v} for v in df.columns.values.tolist()]}
    labels = hlp.get_target_labels()
    metrics = None

    if sess.get_has_targets():
        metrics = get_metrics('classification', sess.get_y_true(), sess.get_y_pred(), labels,
                              logits=sess.get_logits()) if sess.check_key('logits') \
            else get_metrics('regression', sess.get_y_true(), sess.get_y_pred(), labels,
                             target_len=len(hlp.get_targets()))

    return render_template('test_prediction.html', token=session['token'], predict_table=predict_table, metrics=metrics,
                           targets=hlp.get_targets())


@app.route("/deploy", methods=['GET', 'POST'])
@login_required
@check_config
def deploy():
    all_params_config = config_reader.read_config(sess.get_config_file())
    hlp = sess.get_helper()
    export_dir = all_params_config.export_dir()
    checkpoints = run_utils.ckpt_to_table(
        run_utils.get_eval_results(export_dir, sess.get_writer(), sess.get_config_file()))
    all_params_config = run_utils.create_result_parameters(request, sess, checkpoint=checkpoints['Model'].values[-1])
    new_features = hlp.get_new_features(request.form, default_features=True)
    if sess.mode_is_canned():
        all_params_config.set_canned_data(sess.get_canned_data())
    pred = th.predict_estimator(all_params_config, new_features, all=True)
    if pred is None:
        return redirect(url_for('run'))  # flash('Deploy error.', 'error')
    example = hlp.generate_rest_call(pred)

    if request.method == 'POST' and 'model_name' in request.form:
        file_path = sys_ops.export_models(export_dir, get_selected_rows(request), get_model_name(request))
        return send_file(file_path, mimetype='application/zip', attachment_filename=file_path.split('/')[-1],
                         as_attachment=True)
    form = DeploymentForm()
    form.model_name.default = sess.get_model_name()
    form.process()
    return render_template('deploy.html', token=session['token'], checkpoints=checkpoints, page=4,
                           form=form, example=example)


@app.route('/explain_feature', methods=['POST'])
@login_required
@check_config
def explain_feature():
    hlp = sess.get_helper()
    all_params_config = config_reader.read_config(sess.get_config_file())
    all_params_config.set('PATHS', 'checkpoint_dir',
                          os.path.join(all_params_config.export_dir(), get_model(request)))
    file_path, unique_val_column = hlp.create_ice_data(request)
    if sess.mode_is_canned():
        all_params_config.set_canned_data(sess.get_canned_data())
    final_pred = th.predict_test_estimator(all_params_config, file_path)
    if final_pred is None:
        return jsonify(data='Error')
    data = hlp.process_ice_request(request, unique_val_column, final_pred)
    return jsonify(data=data)


@app.route('/')
def main():
    return redirect(url_for('login'))


def flash_errors(form):
    for field, errors in form.errors.items():
        for error in errors:
            flash(u"%s" % error)


db.init_app(app)

if __name__ == '__main__':
    sess = Session(app)
    app.run(debug=True, threaded=True, host='0.0.0.0')
    # app.run(debug=True, threaded=True)
