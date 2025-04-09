import datetime

from logs.status_logger import logger

FILENAME= 'src\info_date\date.csv'

def write_new_date(date):
    try:
        with open(FILENAME, 'w') as file:
            file.write(f"{date}")
            logger("info",f"New date written to date file: {date}")
    except Exception as e:
        logger("error",f"Error writing to date file - {FILENAME}: {e}")