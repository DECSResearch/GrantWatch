# GrantWatch
GrantWatch is a Python-based monitoring bot that continuously downloads, filters, and summarizes grant opportunities, then notifies users via email. It applies a series of date- and content-based checks before delivering only the most relevant, up-to-date listings.

---

- **Automated Scraping** of grant listings (JSON)  
- **Date Filtering**  
  - Discards entries missing or past their `POSTED_DATE`  
  - Optionally removes “Forecasted” opportunities  
- **Keyword Filtering** via LLM-generated keywords on `FUNDING_DESCRIPTION`  
- **LLM Summarization** of filtered grants into concise descriptions  
- **Sorting** by most-recent `POSTED_DATE`  
- **Gmail Notification** of final results  
- **Extensible Structure** for future vector-store / agent integration

---

## Installation & Usage
1. **Check and edit** your `config.yaml`.
2. **Run** the main.py:

   ```bash
   python3 main.py
   ```
## 📝 Logging & Debugging

* All pipeline steps log to the console (and optionally to `logs/`).
* On error, the bot exits with a descriptive message.
* Check `logs/` for history of runs and retained percentages.


## Flowchat
![Mermaid Chart_GrantWatch](https://github.com/user-attachments/assets/2cada1ec-d5ce-4e27-b87f-af1f8363abd1)
