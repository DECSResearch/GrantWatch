from grants_data.download_json import gen_grants
from  grants_data.get_json_data import process_json_data
from grants_data.get_file_path import get_latest_file_path
from grants_data.date_filter_data import date_filter_json_data

from logs.status_logger import logger

def onlyTheGoodStuff():
    #success = gen_grants()
    #if not success:
    #    logger("error", "Failed to generate grants data.")
    #    return success

    latest_file_path = get_latest_file_path()
    if latest_file_path== None:
        logger("error", "No latest file path found.")
        return False

    whole_json_data=process_json_data(latest_file_path)
    if len(whole_json_data) == 0:
        logger("error", "Failed to process JSON data.")
        return False
    
    date_sorted_data = date_filter_json_data(whole_json_data)
    if len(date_sorted_data) == 0:
        logger("warning", "No data found after date filtering.")
        return False
    
    