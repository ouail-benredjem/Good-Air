FROM apache/airflow:2.10.4

USER airflow

# Copier le fichier requirements.txt pour installer d'autres dépendances Python
COPY requirements.txt /requirements.txt
RUN pip install scikit-learn
RUN pip install --no-cache-dir -r /requirements.txt