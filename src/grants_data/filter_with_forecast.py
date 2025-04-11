from logs.status_logger import logger

def filter_forecasted_data(date_sorted_data):
    try:
        status_sorted_data = [item for item in date_sorted_data if item['OPPORTUNITY_STATUS'] != 'Forecasted']
        if len(status_sorted_data) == 0:
            logger("info", "No data found after status filtering.")
        else:
            logger("info", f"Filtered forecast data count: {len(status_sorted_data)}")
        return status_sorted_data
    except KeyError as e:
        logger("error", f"KeyError: {e}. OPPORTUNITY_STATUS key not found in JSON data.")
        return date_sorted_data