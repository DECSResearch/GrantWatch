from grants_data.download_json import gen_grants
from  grants_data.get_json_data import process_json_data
from grants_data.get_file_path import get_latest_file_path
from grants_data.date_filter_data import date_filter_json_data
from grants_data.keyword_filter_data import filter_grants_by_keywords

from llm_utils.keywords_gen import keyword_extractor

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
    del whole_json_data
    if len(date_sorted_data) == 0:
        logger("warning", "No data found after date filtering.")
        return False
    
    keywords , threshold = keyword_extractor()
    if keywords == None:
        logger("error", "Failed to extract keywords.")
        return False
    
    ## filter for title, category, and description
    
    ######################TO-DO#######################
    
    keyword_json_data= filter_grants_by_keywords(date_sorted_data, keywords , threshold)
    del date_sorted_data
    if len(keyword_json_data) == 0:
        logger("warning", "No data found after keyword filtering.")
        return False
    logger("info", f"Filtered JSON data length: {len(keyword_json_data)}")
    
    #from pprint import pprint
    #import json
    #
    #with open("src/grants_data/grants_json_data/filtered_grants.json", "w", encoding="utf-8") as f:
    #    json.dump(keyword_json_data, f, ensure_ascii=False, indent=4)
    
    final_json_data = keyword_json_data
    
    final_json_data.sort(key=lambda x: x['POSTED_DATE'], reverse=True)
    logger("info", f"Sorted JSON data length: {len(keyword_json_data)}")
    
    
    
    #######################################################
    
    return final_json_data
    