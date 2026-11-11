"""Deterministic skill categorization for the canonical schema.

The canonical Skills Intelligence has fine-grained categories
(programming languages, frameworks, ...). The extraction's own
buckets (technical / tools / soft / taxonomy / other) are coarser.
This module maps KNOWN skill names to the fine categories using a
curated dictionary — deterministic, reviewable, and honest:

- A skill found in the dictionary goes to its category.
- A skill NOT in the dictionary stays in `uncategorized` (or in
  `technical_skills` when the source bucket was technical_skills).
  We never guess a category: a wrong category is worse than none,
  because analytics would silently miscount it.

The dictionary deliberately covers the technologies present in the
project's role taxonomy and common resume vocabulary. Extend it by
adding entries — matching is case/punctuation-insensitive.
"""

from __future__ import annotations

import re

_CANONICAL: dict[str, str] = {}


def _add(category: str, names: list[str]) -> None:
    for name in names:
        _CANONICAL[_key(name)] = category


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", " ", str(name).lower()).strip()


_add("programming_languages", [
    "python", "java", "javascript", "typescript", "c", "c++", "c#",
    "go", "golang", "rust", "php", "ruby", "kotlin", "swift", "scala",
    "r", "sql", "pl/sql", "html", "css", "bash", "shell scripting",
    "perl", "matlab", "dart",
])
_add("frameworks", [
    "fastapi", "django", "flask", "spring", "spring boot", "react",
    "react.js", "angular", "vue", "vue.js", "next.js", "node.js",
    "express", "express.js", ".net", "asp.net", "laravel", "rails",
    "ruby on rails", "streamlit", "gradio", "langchain", "langgraph",
    "crewai", "autogen", "llamaindex",
])
_add("libraries", [
    "numpy", "pandas", "matplotlib", "seaborn", "plotly", "requests",
    "beautifulsoup", "beautiful soup", "selenium", "opencv", "pillow",
    "pydantic", "sqlalchemy", "openpyxl", "pytest", "unittest",
    "redux", "jquery", "bootstrap", "tailwind", "tailwind css",
])
_add("databases", [
    "mysql", "postgresql", "postgres", "sqlite", "mongodb", "redis",
    "elasticsearch", "cassandra", "oracle", "sql server", "mariadb",
    "dynamodb", "firebase", "supabase", "faiss", "pinecone", "chroma",
    "chromadb", "milvus", "neo4j", "snowflake", "bigquery",
])
_add("cloud_platforms", [
    "aws", "amazon web services", "azure", "microsoft azure", "gcp",
    "google cloud", "google cloud platform", "heroku", "vercel",
    "netlify", "digitalocean", "cloudflare",
])
_add("devops_tools", [
    "docker", "kubernetes", "git", "github", "gitlab", "bitbucket",
    "jenkins", "ci/cd", "github actions", "terraform", "ansible",
    "linux", "nginx", "apache", "postman", "swagger", "jira",
])
_add("ai_ml_skills", [
    "machine learning", "deep learning", "nlp", "natural language"
    " processing", "computer vision", "tensorflow", "keras",
    "pytorch", "scikit-learn", "sklearn", "xgboost", "lightgbm",
    "catboost", "llm", "llms", "large language models", "rag",
    "retrieval augmented generation", "prompt engineering",
    "embeddings", "vector search", "ai agents", "agentic ai",
    "generative ai", "genai", "fine tuning", "transformers",
    "hugging face", "ollama", "openai api", "data science",
    "statistical analysis", "a/b testing", "feature engineering",
    "model deployment", "mlops",
])
_add("software_tools", [
    "excel", "microsoft excel", "power bi", "tableau", "figma",
    "photoshop", "illustrator", "wordpress", "jupyter",
    "jupyter notebook", "vs code", "visual studio code", "intellij",
    "pycharm", "eclipse", "android studio", "xampp", "n8n",
    "powerpoint", "ms office", "google sheets",
])
_add("domain_skills", [
    "data analysis", "data visualization", "data cleaning", "etl",
    "data pipelines", "api development", "rest apis", "rest api",
    "microservices", "system design", "web development",
    "backend development", "frontend development", "seo",
    "content writing", "content strategy", "digital marketing",
    "social media marketing", "accounting", "financial analysis",
    "electrical wiring", "panel wiring", "fault diagnosis",
    "preventive maintenance", "electrical maintenance", "plc",
    "industrial automation", "quality assurance", "manual testing",
    "automation testing", "test cases",
])

# The extraction's own 'tools' bucket maps here when unrecognized.
_BUCKET_DEFAULT = {
    "technical_skills": "technical_skills",
    "tools": "software_tools",
    "taxonomy_skills": "uncategorized",
    "other_skills": "uncategorized",
}


def categorize(skill_name: str, source_bucket: str) -> str:
    """Fine category for one skill. Dictionary first; otherwise the
    honest fallback for its source bucket; soft skills are handled by
    the caller (their own bucket)."""
    known = _CANONICAL.get(_key(skill_name))
    if known:
        return known
    return _BUCKET_DEFAULT.get(source_bucket, "uncategorized")
