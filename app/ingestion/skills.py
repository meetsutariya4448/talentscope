import re

SKILLS = [
    # Languages
    ("Python", "language"), ("JavaScript", "language"), ("TypeScript", "language"),
    ("Java", "language"), ("Go", "language"), ("Rust", "language"),
    ("C++", "language"), ("C#", "language"), ("Ruby", "language"),
    ("PHP", "language"), ("Swift", "language"), ("Kotlin", "language"),
    ("Scala", "language"), ("R", "language"), ("Bash", "language"),
    ("Shell", "language"), ("SQL", "language"), ("HTML", "language"),
    ("CSS", "language"), ("MATLAB", "language"),
    # Frontend
    ("React", "frontend"), ("Vue", "frontend"), ("Angular", "frontend"),
    ("Next.js", "frontend"), ("Svelte", "frontend"), ("Webpack", "frontend"),
    ("Vite", "frontend"), ("Chart.js", "frontend"), ("D3.js", "frontend"),
    ("Tailwind", "frontend"), ("Bootstrap", "frontend"), ("Redux", "frontend"),
    # Backend
    ("Node.js", "backend"), ("Express", "backend"), ("Django", "backend"),
    ("Flask", "backend"), ("FastAPI", "backend"), ("Spring", "backend"),
    ("Rails", "backend"), ("Laravel", "backend"), (".NET", "backend"),
    ("ASP.NET", "backend"), ("GraphQL", "backend"), ("REST", "backend"),
    ("gRPC", "backend"), ("Celery", "backend"), ("RabbitMQ", "backend"),
    # Databases
    ("PostgreSQL", "database"), ("MySQL", "database"), ("SQLite", "database"),
    ("MongoDB", "database"), ("Redis", "database"), ("Elasticsearch", "database"),
    ("Cassandra", "database"), ("DynamoDB", "database"), ("BigQuery", "database"),
    ("Snowflake", "database"), ("Redshift", "database"), ("Oracle", "database"),
    ("Neo4j", "database"), ("InfluxDB", "database"),
    # Cloud/Infra
    ("AWS", "cloud"), ("GCP", "cloud"), ("Azure", "cloud"),
    ("Docker", "infra"), ("Kubernetes", "infra"), ("Terraform", "infra"),
    ("Ansible", "infra"), ("Jenkins", "infra"), ("GitHub Actions", "infra"),
    ("CircleCI", "infra"), ("Nginx", "infra"), ("Linux", "infra"),
    ("Helm", "infra"), ("Prometheus", "infra"), ("Grafana", "infra"),
    # Data/ML
    ("Spark", "data"), ("Hadoop", "data"), ("dbt", "data"),
    ("Airflow", "data"), ("Databricks", "data"), ("Tableau", "data"),
    ("Power BI", "data"), ("Looker", "data"), ("ETL", "data"),
    ("Pandas", "ml"), ("NumPy", "ml"), ("TensorFlow", "ml"),
    ("PyTorch", "ml"), ("Keras", "ml"), ("Scikit-learn", "ml"),
    ("OpenCV", "ml"), ("Hugging Face", "ml"), ("LangChain", "ml"),
    ("Machine Learning", "ml"), ("Deep Learning", "ml"), ("NLP", "ml"),
    ("Computer Vision", "ml"), ("LLM", "ml"), ("RAG", "ml"),
    ("MLflow", "ml"), ("Weights & Biases", "ml"),
    # Practices/Other
    ("Git", "practice"), ("CI/CD", "practice"), ("Agile", "practice"),
    ("Scrum", "practice"), ("Microservices", "practice"), ("REST API", "practice"),
    ("System Design", "practice"), ("API Design", "practice"),
    ("OAuth", "security"), ("JWT", "security"), ("Kafka", "data"),
    ("Flink", "data"), ("Pulsar", "data"), ("ZooKeeper", "infra"),
]


def extract_skills(text: str) -> list[str]:
    """Extract skill names from text using case-insensitive word boundary matching."""
    if not text:
        return []
    found = []
    text_lower = text.lower()
    for skill_name, _ in SKILLS:
        # Use word boundary matching; handle special chars like C++, .NET, Next.js
        pattern = r'(?<![a-zA-Z0-9])' + re.escape(skill_name.lower()) + r'(?![a-zA-Z0-9])'
        if re.search(pattern, text_lower):
            found.append(skill_name)
    return found
