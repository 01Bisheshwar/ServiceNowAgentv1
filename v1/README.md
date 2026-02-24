```markdown
# External AI Agent for ServiceNow (POC)

## Overview
This repository contains a Proof of Concept (POC) for a **governed external AI agent** built using FastAPI.  
The agent helps users plan and execute ServiceNow configurations in a controlled manner using:
- AI for analysis and planning
- Middleware for validation, approval, and governance
- OAuth-based execution in ServiceNow as the logged-in user

The AI model does **not** directly access ServiceNow. All execution is handled by the middleware.

---

## Prerequisites
Before running this project, ensure the following are installed on your system:

- Python **3.10 or above**
- Git
- A ServiceNow instance (Developer or client instance)
- An AI API key (Gemini / ChatGPT Enterprise for planning)

---

## Project Structure
```

sn_agent/
├── app.py              # FastAPI application entry point
├── agent.py            # AI orchestration and planning logic
├── servicenow.py       # ServiceNow API integration
├── safety.py           # Validation and guardrails
├── static/
│   └── index.html      # Simple UI for POC
├── requirements.txt
├── README.md
└── .env                # Local environment variables (not committed in prod)

````

---

## Step-by-Step: Run the Application Locally

### 1. Clone the repository
```bash
git clone <repository-url>
cd sn_agent
````

---

### 2. Create and activate a virtual environment

```bash
python -m venv .venv
```

Activate the environment:

**Windows**

```bash
.venv\Scripts\activate
```

**Mac/Linux**

```bash
source .venv/bin/activate
```

---

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

### 4. Configure environment variables

Create a `.env` file in the project root.

Example:

```env
OPENAI_API_KEY=your_ai_api_key

SERVICENOW_INSTANCE=https://your-instance.service-now.com
SERVICENOW_CLIENT_ID=your_oauth_client_id
SERVICENOW_CLIENT_SECRET=your_oauth_client_secret
SERVICENOW_REDIRECT_URI=http://localhost:8000/oauth/callback
```

⚠️ Do not commit `.env` to GitHub.
In production, these values should be configured directly in the hosting platform.

---

### 5. Start the FastAPI server

```bash
uvicorn app:app --reload
```

If successful, you should see output similar to:

```
Uvicorn running on http://127.0.0.1:8000
```

---

### 6. Access the application

* Open a browser and navigate to:

  ```
  http://localhost:8000
  ```
* The UI allows you to:

  * Submit requests (e.g., create a catalog item)
  * Upload supporting documents or Excel files
  * Review AI-generated plans before execution

---

## Example Workflow (POC)

1. User submits a request such as:
   **“Create catalog item ‘Hardware Request Form’”**
2. AI analyses the request and generates a proposed plan:

   * Catalog item
   * Variables
   * UI policy
   * Client script
   * Notifications
3. User reviews and approves the plan
4. Middleware executes approved changes in ServiceNow using OAuth
5. User receives confirmation and audit details

---

## OAuth Configuration (ServiceNow)

This POC uses **OAuth 2.0 Authorization Code flow**.

In ServiceNow:

1. Navigate to **System OAuth → Application Registry**
2. Create a new OAuth API endpoint for external clients
3. Set the redirect URI to:

   ```
   http://localhost:8000/oauth/callback
   ```
4. Save and copy the Client ID and Client Secret

---

## Deployment Notes

* `.venv` is for local development only
* Production environments (Render / Hostinger) create their own virtual environment
* Dependencies are installed using `requirements.txt`
* Environment variables are configured in the hosting platform

---

## Security & Governance

* AI is used only for planning and reasoning
* No ServiceNow credentials are exposed to the AI
* All changes require explicit user approval
* Execution happens as the logged-in user via OAuth
* Audit logs are maintained for traceability

---

## Disclaimer

This repository is a Proof of Concept and is not production-ready without additional security hardening, approvals, and monitoring.
```
