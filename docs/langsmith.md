# LangSmith tracing

CareFlow uses LangGraph, which LangSmith traces automatically when tracing is
enabled. No tracing code or additional package is needed.

## Configure

1. Create a LangSmith API key in your LangSmith workspace.
2. Add the values below to the existing, untracked `.env` file. Do not replace
   the other CareFlow settings in that file.

   ```dotenv
   LANGSMITH_TRACING=true
   LANGSMITH_API_KEY=your_langsmith_api_key
   LANGSMITH_PROJECT=careflow-ai-dev
   ```

   The project setting is for project-based workspaces. For an agent-based
   workspace, use `LANGSMITH_AGENT_ID` and `LANGSMITH_AGENT_ENVIRONMENT`
   instead of `LANGSMITH_PROJECT`.

3. If your workspace is outside the default US region, set `LANGSMITH_ENDPOINT`
   to the endpoint for your region.
4. Restart the CareFlow process so it loads the new environment settings, then
   run a synthetic request through `run_request` in `agents/graph.py`.
5. Open the selected project or agent environment in LangSmith and check that
   the run and its nested LangGraph steps appear.

The current `POST /requests` API endpoint only saves a request; it does not
invoke `run_request`. Traces appear only when the graph is actually invoked.

`LANGSMITH_TRACING` is set to `false` in `.env.example` intentionally. The
graph state contains raw patient request text and may include patient details.
LangSmith traces can store run inputs and outputs, so do not enable tracing for
real patient data until your organization has approved the data flow and you
have configured suitable privacy, access, and retention controls. Use synthetic
data while setting up.

The existing `.gitignore` excludes `.env`; keep API keys and other secrets out
of source control.
