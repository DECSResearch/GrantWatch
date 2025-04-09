import json
import os

from info_date import update_date
from info_date import check_new_date

JSON_FILE_PATH = 'src\grants_data\grants_data_20250409_113926.json'
from pprint import pprint


def process_json_data():
    last_date = check_new_date.check_for_new_date()
    if last_date is None:
        update_date.write_new_date()
        

    json_file_path = JSON_FILE_PATH
    
    if not os.path.exists(json_file_path):
        print(f"JSON file not found: {json_file_path}")
        return

    with open(json_file_path, 'r', encoding='utf-8') as json_file:
        json_data = json.load(json_file)

    for item in json_data[:3]:
        pprint(item)  

    update_date.write_new_date()
