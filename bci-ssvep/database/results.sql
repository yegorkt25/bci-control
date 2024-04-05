select cast(sum(json_extract(process_results.results, '$.total_correct_itcca')) as double)/cast(sum(json_extract(process_results.results, '$.total_trials_tested')) as double) as itcca_accuracy,
       cast(sum(json_extract(process_results.results, '$.total_correct_mix')) as double)/cast(sum(json_extract(process_results.results, '$.total_trials_tested')) as double) as mix_accuracy,
       cast(sum(json_extract(process_results.results, '$.total_correct')) as double)/cast(sum(json_extract(process_results.results, '$.total_trials_tested')) as double) as cca_accuracy
from process_results
where parameters_id = :processs_param_id