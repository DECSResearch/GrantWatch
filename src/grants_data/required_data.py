from grants_data.download_json import gen_grants
from  grants_data.get_json_data import process_json_data
from grants_data.get_file_path import get_latest_file_path
from grants_data.date_filter_data import refine_json_data

from logs.status_logger import logger

def onlyTheGoodStuff():
    success = gen_grants()
    if not success:
        logger("error", "Failed to generate grants data.")
        return success

    latest_file_path, date = get_latest_file_path()
    if not latest_file_path:
        logger("error", "No latest file path found.")
        return False

    whole_json_data=process_json_data(latest_file_path)
    if not whole_json_data or len(whole_json_data) == 0:
        logger("error", "Failed to process JSON data.")
        return False
    
    refined_json_data = refine_json_data(whole_json_data, date)
    