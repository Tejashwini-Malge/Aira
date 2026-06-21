import json
import matplotlib.pyplot as plt

# Load the Gemini AI report
with open("latest_report.json", "r") as f:
    report = json.load(f)

# --- Extract Quiz Data ---
quiz_results = report["quiz_results"]
questions = [q["question"] for q in quiz_results["questions"]]
user_answers = [q["user_answer"] for q in quiz_results["questions"]]
correct_answers = [q["correct_answer"] for q in quiz_results["questions"]]
scores = [1 if u==c else 0 for u, c in zip(user_answers, correct_answers)]

# --- Graph 1: Quiz Performance ---
plt.figure(figsize=(10,5))
plt.barh(questions, scores, color=['green' if s==1 else 'red' for s in scores])
plt.xlabel("Correct (1) / Wrong (0)")
plt.title(f"{report['student_name']}'s Quiz Performance")
plt.tight_layout()
plt.show()

# --- Graph 2: Strength vs Weakness ---
strength_count = sum(scores)
weak_count = len(scores) - strength_count

plt.figure(figsize=(6,4))
plt.bar(["Strengths", "Weaknesses"], [strength_count, weak_count], color=["green","orange"])
plt.title("Strength vs Weakness")
plt.show()

# --- Study Plan ---
print("\n--- Personalized Study Plan ---\n")
for step in report["study_plan"]["steps"]:
    print(f"- {step}")

print("\n--- Recommended Flowcharts ---\n")
for flow in report["study_plan"]["flowcharts"]:
    print(f"- {flow}")
