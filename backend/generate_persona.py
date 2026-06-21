import google.generativeai as genai
import json
import os
from dotenv import load_dotenv

load_dotenv()

# Configure API key
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

model = genai.GenerativeModel("models/gemini-2.5-pro")

# Load user answers from JSON
with open("answers.json", "r") as f:
    answers = json.load(f)

# Convert answers dict to prompt text
answers_text = "\n".join([f"{q}: {a}" for q, a in answers.items()])

# Build prompt for Gemini
persona_prompt = f"""
Assume the role of a personalized AI mentor skilled in student assessment. 
Analyze student responses to a provided set of questions (I will provide the questions and answers separately). Based on these answers, deliver a concise personality profile (approximately 50-75 words), identify their dominant learning style (e.g., visual, auditory, kinesthetic, read/write), and describe their likely mindset (e.g., growth-oriented, fixed, optimistic, pragmatic). 
Present this feedback directly to the student in a supportive and encouraging tone, highlighting strengths and suggesting areas for development. 
The goal is to provide actionable self-awareness that promotes effective learning strategies and a positive academic outlook."
Talk directly to the student: "You..." or "You learn..."
- Keep language simple, warm, and human.
- Each field 1–2 sentences max
- Make 'personal_quote' poetic and reflective
""
.

Student Answers:
{answers_text}
"""

# Generate persona
persona_response = model.generate_content(persona_prompt)

persona_text = persona_response.text
print("\n--- PERSONA ---\n")
print(persona_text)

# Save persona to JSON
with open("persona.json", "w") as f:
    json.dump({"persona": persona_text}, f, indent=4)
