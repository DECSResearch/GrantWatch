import datetime

from logs.status_logger import logger


def write_new_date(file_name):
    try:
        with open(file_name, 'w') as file:
            date = datetime.datetime.now().date()
            date = date.strftime("%Y-%m-%d")
            file.write(f"{date}")
            logger("info",f"New date written to file: {date}")
            return True
    except Exception as e:
        logger("error",f"Error writing to file - {file_name}: {e}")
        return False  