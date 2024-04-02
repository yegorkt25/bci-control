import concurrent.futures
import functools
import logging
import operator
import os
import time
from logging import handlers

import numpy as np
import requests
from sklearn.cross_decomposition import CCA
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.db_entities import Recording, ProcessParameters, ProcessResult

log_folder = os.path.abspath(r'../../logs')

log = logging.getLogger(__name__)
logging.basicConfig(encoding='utf-8',
                    format='%(asctime)s.%(msecs)03d :: %(levelname)-8s :: [%(filename)s:%(lineno)d] %(funcName)s - %(message)s',
                    datefmt='%Y-%m-%d:%H:%M:%S',
                    level=logging.INFO)

log_file = os.path.join(log_folder, f"log_{os.path.basename(__file__)[:-3]}_{time.strftime('%Y-%m-%d_%H-%M-%S')}.log")
file_handler = handlers.RotatingFileHandler(log_file)
file_handler.setFormatter(logging.root.handlers[0].formatter)
log.addHandler(file_handler)

np_base_url = 'http://127.0.0.1:6937'
pipeline_file = os.path.abspath(r'../pipelines/SSVEP Sequential.pyp')
max_threads = os.cpu_count()

database_url = 'sqlite:///../../database/data/ssvep-db.sqlite'
database_recordings_folder = os.path.abspath(r'../../database/data/recordings')
database_temp_folder = os.path.abspath(r'../../database/data/temp')

engine = create_engine(database_url)


def calculate_cca(ts_1, ts_2):
    time_series_1 = ts_1
    time_series_2 = ts_2

    cca = CCA(n_components=1)
    X = time_series_1.reshape(-1, 1)
    Y = time_series_2.reshape(-1, 1)
    cca.fit(X, Y)

    X_c, Y_c = cca.transform(X, Y)

    canonical_corr = np.corrcoef(X_c.T, Y_c.T)[0, 1]
    return canonical_corr


def export_csv_for_recording(np_executions_url, pipeline_path, file, frequencies, seconds, save_dir):
    frequencies_rule = []

    for freq in frequencies:
        frequencies_rule.append({'name': f'{freq}hz.csv',
                                 'rule': {f'trial-start-{freq}-hz': 'target-trial-begin',
                                          f'trial-end-{freq}-hz': 'target-trial-end'}})

    params_for_np = {
        'filename': file,
        'hz': frequencies_rule,
        'secs': seconds,
        'path': save_dir
    }

    pipeline_id = requests.post(np_executions_url, json={}).json()['id']
    requests.post(url=f'{np_executions_url}/{pipeline_id}/actions/load', json={'file': pipeline_path, 'what': 'graph'})
    requests.post(url=f'{np_executions_url}/{pipeline_id}/actions/load', json={'what': 'parameters',
                                                                               'data': {
                                                                                   'data': {'value': params_for_np}}})
    requests.patch(url=f'{np_executions_url}/{pipeline_id}/state', json={'running': True, 'paused': False})

    while True:
        get_info = requests.get(url=f'{np_executions_url}/{pipeline_id}/state').json()
        if get_info['completed']:
            break
        time.sleep(0.1)

    requests.delete(url=f'{np_executions_url}/{pipeline_id}')


def get_recordings(session: Session) -> list[Recording]:
    stmt = select(Recording).where(
        (Recording.parameters['duration_s'].as_float() == 6.0)
        & (Recording.parameters['frequencies'].as_string() == '["9","16","13"]')
    )

    result = list(session.scalars(stmt).all())
    log.info(f"Selected {len(result)} recordings to be processed")
    return result


def run_process_and_get_results_for_recording(recording: Recording, params: dict) -> (dict, str, dict):
    result = {'calculations': []}
    frequencies_timeseries = {}

    folder_to_csv = os.path.join(database_temp_folder, recording.xdf_file_path[:-4])

    for i in params['frequencies']:
        #  [1:, 1:] needed to cut header and first column
        frequencies_timeseries[f'{i}hz'] = np.loadtxt(f'{folder_to_csv}\\{i}hz.csv', delimiter=",", dtype=str)[1:, 1:]

    calibrations = {}
    for i in params['frequencies']:
        calibrations[f'{i}hz_1'] = frequencies_timeseries[f'{i}hz'][0]
        calibrations[f'{i}hz_2'] = frequencies_timeseries[f'{i}hz'][1]

    trials_to_test = {}
    for key in frequencies_timeseries.keys():
        trials_to_test[key] = frequencies_timeseries[key][2:]

    for freq_key, freq_time_series_arrays in trials_to_test.items():
        for freq_key_time_series in freq_time_series_arrays:
            trial_results = {}
            for i in params['frequencies']:
                trial_results[f'{i}hz'] = (calculate_cca(calibrations[f'{i}hz_1'],
                                                         freq_key_time_series) + calculate_cca(
                    calibrations[f'{i}hz_2'], freq_key_time_series)) / 2

            guessed_frequency = max(trial_results.items(), key=operator.itemgetter(1))[0]
            result['calculations'].append({'targetFrequency': freq_key,
                                           'calculationsResults': trial_results,
                                           'guessedFrequency': guessed_frequency,
                                           'isCorrect': 1 if guessed_frequency == freq_key else 0})

    result['total_trials_tested'] = len(result['calculations'])
    result['total_correct'] = functools.reduce(lambda current_total, k: current_total + k['isCorrect'],
                                               result['calculations'], 0)
    result['accuracy'] = result['total_correct'] / result['total_trials_tested']

    notes = None
    meta = None
    return result, notes, meta


def run_cca_np_fir():
    log.info("Start processing...")

    with Session(engine) as session, session.begin():
        process_parameters = ProcessParameters.find_proc_params_by_params(parameters, session=session)
        if process_parameters is None:
            process_parameters = ProcessParameters(parameters=parameters)
            session.add(process_parameters)
            session.flush()
            log.info(f"Process parameters did not exist. Created {process_parameters}")
        else:
            log.info(f"Found existing process parameters - {process_parameters}")

        # Prepare csv files after segmentation in temp folder
        recordings = get_recordings(session)
        np_executions_url = np_base_url + '/executions'

        with concurrent.futures.ProcessPoolExecutor(max_workers=max_threads) as executor:
            log.info(f"Submit {len(recordings)} *.xdf files to be processed into CSV through Neuropype")
            for recording in recordings:
                abs_xdf_file_path = os.path.join(database_recordings_folder, recording.xdf_file_path)
                folder_name = recording.xdf_file_path[:-4]
                folder_to_save_csv = os.path.join(database_temp_folder, folder_name)
                executor.submit(export_csv_for_recording,
                                #  export_csv_for_recording arguments:
                                np_executions_url, pipeline_file, abs_xdf_file_path,
                                parameters['frequencies'], parameters['segmentTimeLimits'],
                                folder_to_save_csv)

            log.info(f"Wait for executor to finish export of CSVs")
            executor.shutdown(wait=True)
            log.info(f"CSVs exported and saved into: {database_temp_folder}")

        future_results = {}
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_threads) as executor:
            for recording in recordings:
                future_result = executor.submit(run_process_and_get_results_for_recording,
                                                #  run_process_and_get_results_for_recording arguments:
                                                #  parameters have to be provided because in executor parameters scope changes
                                                recording, parameters)
                future_results[recording] = future_result

            log.info(f"Wait for executor to finish CCA analysis on each recording ({len(recordings)} items)")
            executor.shutdown(wait=True)
            log.info(f"CCA analysis for each recording is finished, processed {len(future_results)} recordings")

        updated = 0
        inserted = 0
        for recording, future_result in future_results.items():
            result, notes, meta = future_result.result()
            process_result = ProcessResult.find_proc_result_by_params_id_and_recording_id(process_parameters.id,
                                                                                          recording.id,
                                                                                          session=session)
            if process_result is None:
                process_result = ProcessResult()
                process_result.parameters_id = process_parameters.id
                process_result.recording_id = recording.id
                process_result.results = result
                process_result.notes = notes
                process_result.meta = meta
                session.add(process_result)
                inserted = inserted + 1
            else:
                process_result.results = result
                process_result.notes = notes
                process_result.meta = meta
                updated = updated + 1

        session.flush()
        log.info(f"Inserted new {inserted} results. Updated {updated} results")


if __name__ == '__main__':
    log.info("=" * 160)
    log.info("Init processing...")
    parameters = {
        'method': 'sklearn CCA, n_components=1, first 2 vs rest averaged, preprocessing in Neuropype',
        'frequencies': [9, 16, 13],
        'segmentTimeLimits': [0.14, 6],
        'trialSequence': [9, 16, 13] * 8
    }
    log.info(f"Log folder: {log_folder}")
    log.info(f"Database recordings folder: {database_recordings_folder}")
    log.info(f"Database temp folder: {database_temp_folder}")
    log.info(f"Neuropype base url: {np_base_url}")
    log.info(f"Pypeline file: {pipeline_file}")
    log.info(f"Max threads: {max_threads}")
    log.info(f"Parameters: {parameters}")
    run_cca_np_fir()