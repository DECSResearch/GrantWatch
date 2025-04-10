import datetime

from info_date import update_date
from info_date import check_new_date

from logs.status_logger import logger

DATE_FILE = 'src/grants_data/timeline/date.csv'

def get_and_update_date():
    logger("info", "Getting and updating date...")
    last_date = None
    last_date = check_new_date.check_for_new_date()
    logger("debug", f"Last date from file: {last_date}")
    current_date = datetime.datetime.now().date()
    current_date = current_date.strftime("%Y-%m-%d")
    
    if last_date!= None:
        update_date.write_new_date(current_date)
        return last_date
    else:
        update_date.write_new_date(current_date)
        last_date = datetime.datetime.min
        last_date = last_date.date()
        logger("info", f"Last date updated to min: {last_date}")
        return last_date
    
def date_filter_json_data(json_data):
    try:
        logger("info", "Refining JSON data...")
        if not json_data or len(json_data) == 0:
            logger("error", "No JSON data to refine.")
            return []
        date=get_and_update_date()
        refined_data = []
        logger("info", f"Filtering JSON data for date: {date}...")
        for item in json_data:
            posted_date = str(item.get('POSTED_DATE'))
            posted_date = posted_date.replace("/", "-")
            if posted_date:
                posted_date = datetime.datetime.strptime(posted_date, "%m-%d-%Y").date()
            else:
                item['POSTED_DATE'] = None

            if posted_date >= date:
                refined_data.append(item)
        del json_data
        logger("info", f"Filtered JSON data length: {len(refined_data)}")
        return refined_data
    except Exception as e:
        logger("error", f"Error refining JSON data: {e}")
        return []