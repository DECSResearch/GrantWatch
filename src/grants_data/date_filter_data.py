from info_date import update_date
from info_date import check_new_date

def check_and_update_date():
    file_name = 'src/grants_data/timeline/date.csv'
    last_date = check_new_date.check_for_new_date()
    
    if last_date:
        current_date = datetime.datetime.now().date()
        if last_date != current_date:
            update_date.write_new_date(file_name)
            return True
        else:
            return False
    else:
        update_date.write_new_date(file_name)
        return True
    
def refine_json_data(json_data, date):
    refined_data = []
    for item in json_data:
        if item.get('date') == date:
            refined_data.append(item)
    return refined_data