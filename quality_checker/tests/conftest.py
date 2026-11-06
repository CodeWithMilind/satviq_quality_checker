"""Shared synthetic test data — no external files or network access."""

import pytest

NORMAL_RESUME = """Milind Chaudhari
milind@example.com | +91 98765 43210 | Pune, India

Contact Information
Email: milind@example.com, Phone: +91 98765 43210, Location: Pune, India.

Professional Summary
Python developer and MCA student building practical AI and backend applications for real users.
Focused on FastAPI services, data pipelines, and LLM powered tools with clean, tested code.

Skills
Python, FastAPI, REST APIs, SQL, MySQL, Pandas, NumPy, LangChain, and Git are used daily.
Built backend services with authentication, validation, background tasks, and structured logging.

Work Experience
Data Scientist Intern at Rubixe AI Solutions from May 2025 to November 2025 in Bengaluru.
Cleaned datasets, trained classification models, and documented experiments for the team.

Education
Master of Computer Applications at DPGU School of Management and Research, Pune, 2025 to 2027.
Bachelor of Computer Applications at MJ College, Jalgaon, completed in 2025 with distinction.

Projects
Data4Viz is a FastAPI and Next.js workbench for uploading, analysing, and visualising datasets.
PDF Chat Assistant answers questions over documents using RAG, LangChain, FAISS, and FastAPI.

Certifications
NASSCOM Certified Data Scientist and IABAC Certified Data Scientist, completed in 2025.
Completed additional coursework in machine learning, statistics, and Python programming.
"""

PARTIAL_RESUME = """Jane Doe
jane@example.com

Skills
Python, SQL, Excel, and data visualisation tools used for reporting and analysis work.

Education
Bachelor of Science in Computer Science from a recognised university, completed in 2024.
Studied databases, algorithms, operating systems, statistics, and software engineering.
"""

ALTERNATIVE_HEADINGS_RESUME = """John Smith
john.smith@example.com | +1 555 010 2233

Technical Skills
Java, Spring Boot, REST APIs, PostgreSQL, Docker, and automated testing frameworks.

Professional Experience
Software Engineer Intern at Example Labs from June 2024 to December 2024 in a remote team.
Shipped backend features, fixed production bugs, and wrote integration tests for services.

Academic Background
Master of Science in Information Systems from an accredited university, completed in 2026.
Focused on distributed systems, databases, cloud computing, and applied machine learning.
"""

FRAGMENTED_TEXT = "\n".join(
    ["The", "quick", "brown", "fox", "jumps", "over", "the", "lazy", "dog", "and",
     "then", "runs", "far", "away", "from", "the", "house", "every", "single", "day"]
)

CORRUPTED_TEXT = ("Normal sentence with readable words in it. " * 4) + ("�" * 120)

SYMBOL_HEAVY_TEXT = ("Readable words appear here in a normal sentence structure. " * 3) + ("★☀☁☂☃★☀☁☂☃" * 40)

WHITESPACE_HEAVY_TEXT = "word" + (" " * 600) + "another" + ("\t" * 200) + "text"


@pytest.fixture()
def normal_resume() -> str:
    return NORMAL_RESUME


@pytest.fixture()
def partial_resume() -> str:
    return PARTIAL_RESUME
