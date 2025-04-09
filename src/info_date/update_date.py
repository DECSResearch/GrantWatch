import datetime

import status_logger

FILENAME= 'src/grants_data/timeline/date.csv'

def write_new_date():
    try:
        with open(FILENAME, 'w') as file:
            date = datetime.datetime.now().date()
            date = date.strftime("%Y-%m-%d")
            file.write(f"{date}")
            status_logger.logger("info",f"New date written to file: {date}")
            return True
    except Exception as e:
        status_logger.logger("error",f"Error writing to file - {FILENAME}: {e}")
        return False  