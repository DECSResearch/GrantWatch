from grants_data.download_json import gen_grants
from  grants_data.get_json_data import process_json_data
from grants_data.get_file_path import get_latest_file_path
from grants_data.date_filter_data import date_filter_json_data
from grants_data.keyword_filter_data import filter_grants_by_keywords
from grants_data.filter_with_forecast import filter_forecasted_data

from llm_utils.keywords_gen import keyword_extractor
from llm_utils.gpt_summarizer import description_summarizer

from logs.status_logger import logger


def onlyTheGoodStuff():
    #success = gen_grants()
    #if not success:
    #    logger("error", "Failed to generate grants data.")
    #    return success

    latest_file_path = get_latest_file_path()
    if latest_file_path== None:
        logger("error", "No latest file path found.")
        return False , None

    whole_json_data=process_json_data(latest_file_path)
    length_initial = len(whole_json_data)
    if len(whole_json_data) == 0:
        logger("error", "Failed to process JSON data.")
        return False , None
    
    date_sorted_data = date_filter_json_data(whole_json_data)
    del whole_json_data
    if len(date_sorted_data) == 0:
        logger("warning", "No data found after date filtering.")
        return False , None
    
    keywords , threshold , forecast = keyword_extractor()
    if keywords == None:
        logger("error", "Failed to extract keywords.")
        return False , None
    
    
    if not forecast:
        logger("info", "Forecast is set to False. Filtering grants with OPPORTUNITY_STATUS = 'Forecasted'")
        status_sorted_data = filter_forecasted_data(date_sorted_data)
        if len(status_sorted_data) == 0:
            logger("info", "No data found after status filtering.")
            return False , None
    else:
        logger("info", "Forecast is set to True. Keeping all data.")
        status_sorted_data = date_sorted_data

    del date_sorted_data    
    
    ## filter for title, category, and description
    
    ######################TO-DO#######################
    
    keyword_json_data= filter_grants_by_keywords(status_sorted_data,"FUNDING_DESCRIPTION", keywords , threshold)
    del status_sorted_data
    if len(keyword_json_data) == 0:
        logger("warning", "No data found after keyword filtering.")
        return False , None
    logger("info", f"Filtered keyword length: {len(keyword_json_data)}")
    
    from pprint import pprint
    #import json
    #
    #with open("src/grants_data/grants_json_data/filtered_grants.json", "w", encoding="utf-8") as f:
    #    json.dump(keyword_json_data, f, ensure_ascii=False, indent=4)
    
    summarized_json_data = description_summarizer(keyword_json_data)
    if summarized_json_data == None:
        logger("error", "Failed to summarize descriptions.")
        return False , None

    final_json_data = summarized_json_data
    
    final_json_data.sort(key=lambda x: x['POSTED_DATE'], reverse=True)
    logger("info", "Sorted JSON data")
    
    
    
    #######################################################
    
    final_length = len(final_json_data)
    
    logger("info", f"Initial by final length: {length_initial} / {final_length}")
    logger('info', f"Percentage of data retained: {round((final_length/length_initial)*100, 2)}%")
    
    return True, final_json_data
    