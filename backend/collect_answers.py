import requests
import json
import getpass

BASE_URL = "http://127.0.0.1:5000"

# A single Session keeps the login cookie so protected routes (save-answers) work.
session = requests.Session()

# -------------------------
# Step 0: Log in
# (save-answers is now per-user and requires an authenticated session)
# -------------------------
email = input("Email: ").strip()
password = getpass.getpass("Password: ")
try:
    res = session.post(f"{BASE_URL}/login", json={"email": email, "password": password})
    if not res.ok or not res.json().get("success"):
        print("Login failed:", res.json().get("message", res.status_code))
        exit(1)
    print(f"Logged in as {email}\n")
except Exception as e:
    print("Could not reach the server:", e)
    exit(1)

# -------------------------
# Step 1: Fetch questions
# -------------------------
try:
    res = session.get(f"{BASE_URL}/session/get-questions")
    res.raise_for_status()
    questions = res.json()["questions"]
except Exception as e:
    print("Error fetching questions:", e)
    exit(1)

print("\nAnswer the following questions to generate your persona\n")

# -------------------------
# Step 2: Collect answers
# -------------------------
answers = {}

for q in questions:
    qid = q["id"]
    text = q["text"]
    ans = input(f"{qid}. {text}\n-> Your answer: ").strip()
    answers[f"Q{qid}"] = ans

# -------------------------
# Step 3: Show confirmation
# -------------------------
print("\nCollected answers successfully!\n")
for k, v in answers.items():
    print(f"{k}: {v}")

# -------------------------
# Step 4: Save locally (debug)
# -------------------------
with open("answers.json", "w", encoding="utf-8") as f:
    json.dump(answers, f, ensure_ascii=False, indent=2)

print("\nAnswers saved to answers.json")

# -------------------------
# Step 5: Send to backend (authenticated session)
# -------------------------
try:
    res = session.post(f"{BASE_URL}/session/save-answers", json={"answers": answers})
    res.raise_for_status()
    print("\nAnswers sent to backend successfully!")
except Exception as e:
    print("Error sending answers:", e)
