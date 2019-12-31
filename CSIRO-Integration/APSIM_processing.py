import sys
import os
import warnings
sys.path.append("../db")

if not sys.warnoptions:
    warnings.simplefilter("ignore")

from database import init_db, db_session
from models import Metadata, Output, Parameters

import pandas as pd
import geopandas as gpd
import numpy as np
import shapely.geometry
import json
import yaml
import configparser
import redis
import boto3
from datetime import datetime
from collections import OrderedDict
from hashlib import sha256

config = configparser.ConfigParser()
config.read('../REST-Server/config.ini')

r = redis.Redis(host=config['REDIS']['HOST'],
                port=config['REDIS']['PORT'],
                db=config['REDIS']['DB'])

profile = "wmuser"
bucket = "world-modelers"

session = boto3.Session(profile_name=profile)
s3 = session.resource("s3")
s3_client = session.client("s3")
s3_bucket= s3.Bucket(bucket)

class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        else:
            return super(NpEncoder, self).default(obj)

def generate_geojson(geom):
    return shapely.geometry.mapping(geom)


def generate_random(polygon):
    minx, miny, maxx, maxy = polygon.bounds
    counter = 0
    while True:
        pnt = Point(random.uniform(minx, maxx), random.uniform(miny, maxy))
        if polygon.contains(pnt):
            return pnt


# This needs to be updated based on a lookup from CSIRO
def gridcell_to_loc(row):
    point = generate_random(eth)
    row['geometry'] = point
    row['latitude'] = point.y
    row['longitude'] = point.x
    return row


def gen_run(model_name, params):
    
    params.pop('scenario')
    params.pop('description')
    
    model_config = {
                    'config': params,
                    'name': model_name
                   }

    model_config = sortOD(OrderedDict(model_config))
    run_id = sha256(json.dumps(model_config, cls=NpEncoder).encode('utf-8')).hexdigest()

    # Add to model set in Redis
    r.sadd(model_name, run_id)
    
    run_obj = {'status': 'SUCCESS',
     'name': model_name,
     'config': model_config["config"],
     'bucket': bucket,
     'key': f"results/{model_name}_results/{run_id}.csv"
    }

    run_obj['config']['run_id'] = run_id
    run_obj['config'] = json.dumps(run_obj['config'], cls=NpEncoder)
    
    # Create Redis object
    r.hmset(run_id, run_obj)
    
    return run_id, model_config, run_obj
      

def sortOD(od):
    res = OrderedDict()
    for k, v in sorted(od.items()):
        if isinstance(v, dict):
            res[k] = sortOD(v)
        else:
            res[k] = v
    return res                                


if __name__ == "__main__":

    scenarios = pd.read_csv('Scenarios.csv')

    with open('../metadata/models/APSIM-model-metadata.yaml', 'r') as stream:
        apsim = yaml.safe_load(stream)
        
    with open('../metadata/models/G-Range-model-metadata.yaml', 'r') as stream:
        grange = yaml.safe_load(stream)    

    admin2 = gpd.read_file("gadm36_ETH_shp/gadm36_ETH_2.shp")
    admin2['country'] = admin2['NAME_0']
    admin2['state'] = admin2['NAME_1']
    admin2['admin1'] = admin2['NAME_1']
    admin2['admin2'] = admin2['NAME_2']
    admin2['GID_2'] = admin2['GID_2'].apply(lambda x: x.split("_")[0])
    admin2['GID_1'] = admin2['GID_1'].apply(lambda x: x.split("_")[0])
    admin2['geojson'] = admin2.geometry.apply(lambda x: generate_geojson(x))

    eth = cascaded_union(admin2.geometry)

    crops = pd.read_csv('C2-P2 APSIM-GRange Results v01/Cropping_Grid_Backcast_Experiment_2020-01.csv')

    # add in parameters from scenarios dataframe
    crops = crops.merge(scenarios, on='scenario', how='left', suffixes=(False, False))

    model_name = apsim['id']
    crop_param = [i for i in apsim['parameters'] if i['name']=='crop'][0]
    season_param = [i for i in apsim['parameters'] if i['name']=='season'][0]

    outputs = {}
    for o in apsim['outputs']:
        outputs[o['name']] = o
        
    scenario_list = crops.scenario.unique()

    param_cols = list(scenarios.columns)
    base_cols = ['gridcell_id', 'cropping_year']

    for season_type in season_param['metadata']['choices']:
        for crop_type in crop_param['metadata']['choices']:
            for scen in scenario_list:
                
                # select the correct yield columns for crop/season and rename them
                yield_col = f"yield_{season_type}_{crop_type}"
                anomaly_col = f"rel_anomaly_{yield_col}"
                cols = param_cols + base_cols + [yield_col, anomaly_col]
                crops_ = crops[cols]
                crops_ = crops_.rename(columns={yield_col:'yield',anomaly_col:'rel_anomaly_yield'})
                crops_['datetime'] = crops_.cropping_year.apply(lambda x: datetime(year=x,month=1,day=1))
                
                # subset for the correct scenario
                crops_ = crops_[crops_['scenario'] == scen]
                
                # drop rows where yield fields are NA
                crops_ = crops_.dropna(subset=['yield','rel_anomaly_yield'])
                
                # generate random location information
                # should be updated with CSIRO lookups per gridcell
                crops_ = crops_.apply(gridcell_to_loc, axis=1)
                
                # obtain scenario parameters
                params = scenarios[scenarios['scenario']==scen].iloc[0].to_dict()
                params['crop'] = crop_type
                params['season'] = season_type
                
                run_id, model_config, run_obj = gen_run(model_name, params)
                
                # generate temp CSV and push it to S3
                crops_.to_csv("tmp.csv", index=False)
                s3_bucket.upload_file("tmp.csv", run_obj['key'], ExtraArgs={'ACL':'public-read'})
                
                # Add metadata object to DB
                meta = Metadata(run_id=run_id, 
                                model=model_name,
                                raw_output_link= f"https://model-service.worldmodelers.com/results/{model_name}_results/{run_id}.csv",
                                run_label=crops_.description[0],
                                point_resolution_meters=25000)
                db_session.add(meta)
                db_session.commit()
        
                # Add parameters to DB
                for param in apsim['parameters']:
                    if param['metadata']['type'] == 'ChoiceParameter':
                        p_type = 'string'
                        p_value = params[param['name']]
                    elif param['name'] == 'temperature':
                        p_type = 'float'
                        p_value = float(params[param['name']])
                    else:
                        p_type = 'integer'
                        p_value = int(params[param['name']])

                    param = Parameters(run_id=run_id,
                                      model=model_name,
                                      parameter_name=param['name'],
                                      parameter_value=p_value,
                                      parameter_type=p_type
                                      )
                    db_session.add(param)
                    db_session.commit()
                
                gdf = gpd.GeoDataFrame(crops_)
                gdf = gpd.sjoin(gdf, admin2, how="left", op='intersects')
                gdf['run_id'] = run_id
                gdf['model'] = model_name
                if 'geometry' in gdf:
                    del(gdf['geometry'])
                    del(gdf['index_right'])
                    
                for feature in ['yield','rel_anomaly_yield']:
                    gdf_ = gdf
                    gdf_['feature_name'] = feature
                    gdf_['feature_value'] = gdf_[feature]
                    gdf_['feature_description'] = outputs[feature]['description']
                    
                    db_session.bulk_insert_mappings(Output, gdf_.to_dict(orient="records"))
                    db_session.commit()    