# module1_fetch_questions.py
import requests

def fetch_questions():
    try:
        r = requests.get("http://127.0.0.1:5000/get-questions")
        r.raise_for_status()
        questions = r.json().get("questions", [])
        if not questions:
            print("No questions received.")
            return []
        print("Fetched questions successfully:")
        for i, q in enumerate(questions, 1):
            print(f"{i}. {q}")
        return questions
    except Exception as e:
        print("Error fetching questions:", e)
        return []

if __name__ == "__main__":
    fetch_questions()
