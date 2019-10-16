import os
import redis
import configparser
from hashlib import sha256
import json

config = configparser.ConfigParser()
config.read('../REST-Server/config.ini')

r = redis.Redis(host=config['REDIS']['HOST'],
                port=config['REDIS']['PORT'],
                db=config['REDIS']['DB'])

bucket = 'world-modelers'

crops = ['wheat', 'maize', 'millet']
irrigation = ['LIM', 'NO', 'POT'] 
nitrogen = ['LIM', 'LIM_p25', 'LIM_p50', 'UNLIM']
stats = ['mean', 'std', 'pctl,5', 'pctl,95']

def gen_global(crop, irrig, nit, stat):
    if '_' in nit:
        inc = nit.split('_')[1] + '_'
        nit = nit.split('_')[0]
    else:
        inc = ''
    output = f"global_anomalies_{crop}_{irrig}_IRRIGATION_{nit}_NITROGEN_{inc}{stat}_REFLIM_IRRIGATION_REFLIM_NITROGEN.tif"
    return output

def gen_merged(crop, irrig, nit):
    if '_' in nit:
        inc = nit.split('_')[1] + '_'
        nit = nit.split('_')[0]
    else:
        inc = ''    
    output = f"merged_national_crop_anomalies_{irrig}_IRRIGATION_{nit}_NITROGEN_{inc}REFLIM_IRRIGATION_REFLIM_NITROGEN_{crop}_aggregated.txt"
    return output

def gen_run(crop, irrig, nit, output, stat=None):
    model_name = 'yield_anomalies_lpjml'
    model_config = {
                      "crop": crop,
                      "irrigation": irrig,
                      "nitrogen": nit,
                   }

    if stat == None:
        model_config["area"] = "merged"
    else:
        model_config["area"] = "global"
        model_config["statistic"] = stat

    run_id = sha256(json.dumps(model_config).encode('utf-8')).hexdigest()
    
    # Add to model set in Redis
    r.sadd(model_name, run_id)
    
    run_obj = {'status': 'SUCCESS',
     'name': model_name,
     'config': model_config,
     'bucket': bucket,
     'key': f"results/yield_anomalies_model/{output}"
    }

    run_obj['config']['run_id'] = run_id
    run_obj['config'] = json.dumps(run_obj['config'])
    
    r.hmset(run_id, run_obj)

if __name__ == "__main__":

    for crop in crops:
        for irrig in irrigation:
            for nit in nitrogen:
                # Generate merged runs
                output = gen_merged(crop, irrig, nit)
                gen_run(crop, irrig, nit, output)

                for stat in stats:
                    # Generate global runs
                    output = gen_global(crop, irrig, nit, stat)
                    gen_run(crop, irrig, nit, output, stat)
