
In upcoming releases, we plan to evolve GrantWatch from a simple scraper-notifier bot into an LLM-backed AI agent with Retrieval-Augmented Generation capabilities:

1. **Data Ingestion & Storage**  
   - After scraping, index grant details (title, description, deadlines) in a vector database (e.g. FAISS, Pinecone) using sentence embeddings.

2. **Retrieval Layer**  
   - Embed user queries (e.g. “STEM grants closing in 10 days”) and fetch the top-K matching grant records from the vector store.

3. **Agent Core (LLM + Planner)**  
   - Wrap an LLM (e.g. OpenAI GPT) in an agent framework (e.g. LangChain Agent).  
   - Expose “tools” such as:
     - `search_grants(filters)`: run similarity searches over the vector index  
     - `summarize_grant(id)`: generate concise summaries of grant details  
     - `notify(channel, payload)`: dispatch results via email/Slack  

4. **Execution & Notification**  
   - The agent reasons over retrieved grants, applies any extra filtering or summarization, then calls `notify()` to deliver human-readable reports.

5. **Orchestration & Scheduling**  
   - Integrate agent invocation into your scheduler (cron or APScheduler), with logging of decision traces for auditability.

By modularizing scrapers and notification channels into discrete “tools,” and orchestrating them via a planner-driven LLM, GrantWatch will gain the flexibility and intelligence of an AI agent (akin to the Cyber-Safari framework), enabling complex, context-aware grant discovery and outreach.  
