# Putting DeBet online for free

DeBet will run on **Streamlit Community Cloud** (the app) with **Supabase** (the database).
Both are free. You only keep paying OpenRouter for the AI (about $2–3/month).

---

## Part 1 – Supabase (the database)  ✅ done (19 Nov 2026)

Your project is in **EU West (Ireland)** and your data has been copied in.
To change the address in `.env` later (e.g. after a new database password), right-click
`set_database_url.ps1` → **Run with PowerShell** and paste the new address.

<details><summary>Original steps (for reference)</summary>


1. Go to **supabase.com** → **Start your project** → sign up (GitHub or email).
2. **New project**:
   - Name: `debet`
   - Database password: click **Generate**, then **copy it somewhere safe**.
     *Tip: use letters and numbers only – symbols like `@ # /` break the address later.*
   - Region: **East US (North Virginia)** – closest to Streamlit's servers, so the app is faster.
3. Wait ~1 minute until the project is ready.
4. Click **Connect** (top of the page) → choose **Session pooler** → copy the **URI**. It looks like:
   `postgresql://postgres.abcdefgh:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres`
5. Replace `[YOUR-PASSWORD]` (including the brackets) with your database password.
6. Open the `.env` file in the `debet` folder and add a new line:
   ```
   DATABASE_URL=postgresql://postgres.abcdefgh:yourpassword@aws-0-us-east-1.pooler.supabase.com:5432/postgres
   ```
   Save the file. **Then tell Claude "done"** – Claude copies your data into Supabase and tests it.

> From now on your PC's DeBet uses Supabase too, so phone and PC always show the same data.

</details>

---

## Part 2 – GitHub (where the code lives)  ⏱ ~5 min

1. Go to **github.com** → sign up.
2. **New repository** → name `debet` → choose **Private** → **Create repository**.
3. Click **uploading an existing file** and drag in these from the `debet` folder:
   `app.py`, `database.py`, `debate_engine.py`, `value_engine.py`, `screenshot_reader.py`,
   `result_verifier.py`, `copy_to_supabase.py`, `requirements.txt`, `.gitignore`, `DEPLOY.md`
   and the **`.streamlit` folder** (it holds the colour theme).
   **Never upload `.env` or `debet.db`** – they contain your keys and data.
4. Click **Commit changes**.

---

## Part 3 – Streamlit Cloud (the app)  ⏱ ~5 min

1. Go to **share.streamlit.io** → **Continue with GitHub** → allow access to your `debet` repository.
2. **Create app** → **Deploy a public app from GitHub**:
   - Repository: `yourname/debet`, Branch: `main`, Main file: `app.py`
   - App URL: pick a name, e.g. `my-debet`
3. Open **Advanced settings**:
   - Python version: **3.12**
   - **Secrets** – paste (with your real values, keep the quotes):
     ```
     OPENROUTER_API_KEY = "sk-or-..."
     DATABASE_URL = "postgresql://postgres.abcdefgh:yourpassword@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
     ```
4. **Deploy**. The first start takes 2–3 minutes. Then open `https://my-debet.streamlit.app` on your phone,
   log in, and use **Add to Home screen**.

---

## Good to know

- **Sleeping:** if nobody opens the app for a while, Streamlit puts it to sleep. Opening it shows
  "wake up" – tap it and wait ~30 seconds.
- **Supabase pause:** a free project pauses after ~1 week with no use. Log in to supabase.com and click
  **Restore**. Using DeBet a few times a week prevents this.
- **Keep it private:** in the Streamlit app's **Settings → Sharing**, you can limit who can view it.
  DeBet also has its own login and locks a login for 15 minutes after 5 wrong passwords.
- **Spending cap:** set a monthly limit on openrouter.ai (Settings → Credits) so AI costs can't run away.
- **Updating the app later:** upload the changed files to GitHub again – Streamlit restarts with them.
