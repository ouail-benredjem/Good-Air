from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
import requests
import json
import pandas as pd
from datetime import datetime, timedelta
import logging
from datetime import datetime

# Clés API et villes
AQICN_API_KEY = "3e656615db324d7097f7ad604d1315961c20b4ec"
OPENWEATHER_API_KEY = "1b930df782fb8928244dec558ecb466c"
CITIES = ["Paris", "Lyon", "Lille", "Marseille","Montpellier"]
CSV_FILE = "/opt/airflow/data/merged_data.csv"

# Fonction de récupération des données
def fetch_data(city):
    air_url = f"https://api.waqi.info/feed/{city}/?token={AQICN_API_KEY}"
    weather_url = f"https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={OPENWEATHER_API_KEY}&units=metric"
    
    air_resp = requests.get(air_url).json().get("data", {})
    weather_resp = requests.get(weather_url).json()
    
    return air_resp, weather_resp

# Fonction pour insérer les données brutes dans le Data Lake
def insert_data_lake(city, air_data, weather_data):
    try:
        hook = PostgresHook(postgres_conn_id="postgres_data", database='GoodAir')
        conn = hook.get_conn()
        cursor = conn.cursor()

        # Créer la table Data Lake si elle n'existe pas
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS data_lake (
            id SERIAL PRIMARY KEY,
            city TEXT,
            timestamp TIMESTAMP,
            source TEXT,
            raw_data JSONB
        );
        """)
        conn.commit()

        # Insérer les données brutes (Air Quality + Weather) dans le Data Lake
        raw_data = json.dumps({"air_quality": air_data, "weather": weather_data})  # Combine les données en JSON
        insert_query = """
        INSERT INTO data_lake (city, timestamp, source, raw_data)
        VALUES (%s, %s, %s, %s);
        """
        cursor.execute(insert_query, (city, datetime.now(), "API", raw_data))
        conn.commit()

        logging.info(f"Données brutes insérées pour {city} dans Data Lake")

    except Exception as e:
        logging.error(f"Erreur lors de l'insertion des données dans le Data Lake : {e}")
        raise

# Fonction de transformation des données
def transform_data():
    all_air_quality, all_weather = [], []
    
    for city in CITIES:
        air_quality, weather = fetch_data(city)
        
        # Insérer les données brutes dans le Data Lake
        insert_data_lake(city, air_quality, weather)
        
        # Transformer les données (Nettoyage, Agrégation, etc.)
        air_quality_df = pd.DataFrame([{ 
            "city": city,
            "aqi": air_quality.get("aqi"),
            "pm25": air_quality.get("iaqi", {}).get("pm25", {}).get("v"),
            "pm10": air_quality.get("iaqi", {}).get("pm10", {}).get("v")
        }])
        
        weather_data = [{
            "city": city,
            "timestamp": datetime.utcfromtimestamp(forecast["dt"]),
            "temperature": int(forecast["main"]["temp"]),
            "humidity": forecast["main"]["humidity"],
            "pressure": forecast["main"]["pressure"]
        } for forecast in weather.get("list", [])]
        
        weather_df = pd.DataFrame(weather_data)
        
        all_air_quality.append(air_quality_df)
        all_weather.append(weather_df)
    
    final_air_quality_df = pd.concat(all_air_quality, ignore_index=True)
    final_weather_df = pd.concat(all_weather, ignore_index=True)
    merged_df = pd.merge(final_weather_df, final_air_quality_df, on='city', how='left')
    
    # Colonnes à convertir en numérique
    cols_to_check = ["aqi", "pm25", "pm10", "temperature", "humidity", "pressure"]
    for col in cols_to_check:
        merged_df[col] = pd.to_numeric(merged_df[col], errors='coerce')

    # Supprimer les lignes avec des valeurs NaN dans les colonnes critiques
    before = len(merged_df)
    merged_df = merged_df.dropna(subset=cols_to_check)
    after = len(merged_df)

    if before != after:
        logging.warning(f"{before - after} lignes ont été ignorées à cause de valeurs non valides.")

    # Stocker dans le Data Warehouse
    store_data_warehouse(merged_df)
    logging.info("Données transformées et stockées dans le Data Warehouse")

    return merged_df


# Fonction pour insérer les données transformées dans le Data Warehouse
def store_data_warehouse(df):
    try:
        hook = PostgresHook(postgres_conn_id="postgres_data", database='GoodAir')
        conn = hook.get_conn()
        cursor = conn.cursor()

        # Créer la table Data Warehouse si elle n'existe pas
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS data_warehouse (
            city TEXT,
            timestamp TIMESTAMP,
            aqi FLOAT,
            pm25 FLOAT,
            pm10 FLOAT,
            temperature FLOAT,
            humidity FLOAT,
            pressure FLOAT
        );
        """)
        conn.commit()

        # Insérer les données nettoyées dans le Data Warehouse
        for _, row in df.iterrows():
            row = row.to_dict()  # Convertir en dict

            insert_query = """
            INSERT INTO data_warehouse (city, timestamp, aqi, pm25, pm10, temperature, humidity, pressure)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
            """
            cursor.execute(insert_query, (
                row["city"], row["timestamp"], row["aqi"], row["pm25"], row["pm10"],
                row["temperature"], row["humidity"], row["pressure"]
            ))

        conn.commit()
        logging.info("Données insérées dans le Data Warehouse")

    except Exception as e:
        logging.error(f"Erreur stockage des données dans le Data Warehouse : {e}")
        raise

# Fonction pour créer des tables de Data Mart pour chaque ville
def create_data_mart():
    try:
        hook = PostgresHook(postgres_conn_id="postgres_data", database='GoodAir')
        conn = hook.get_conn()
        cursor = conn.cursor()

        # Créer les tables de Data Mart pour chaque ville
        for city in CITIES:
            cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {city.lower()}_data_mart (
                timestamp TIMESTAMP,
                aqi FLOAT,
                pm25 FLOAT,
                pm10 FLOAT,
                temperature FLOAT,
                humidity FLOAT,
                pressure FLOAT
            );
            """)
        conn.commit()
        logging.info("Tables de Data Mart créées pour chaque ville")

    except Exception as e:
        logging.error(f"Erreur lors de la création des tables de Data Mart : {e}")
        raise

# Fonction pour insérer les données par ville dans les tables de Data Mart
def insert_data_mart(df):
    try:
        hook = PostgresHook(postgres_conn_id="postgres_data", database='GoodAir')
        conn = hook.get_conn()
        cursor = conn.cursor()

        for city in CITIES:
            city_df = df[df["city"] == city]
            
            # Insérer les données dans la table Data Mart spécifique à chaque ville
            for _, row in city_df.iterrows():
                row = row.to_dict()  # Convertir en dict

                insert_query = f"""
                INSERT INTO {city.lower()}_data_mart (timestamp, aqi, pm25, pm10, temperature, humidity, pressure)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
                """
                cursor.execute(insert_query, (
                    row["timestamp"], row["aqi"], row["pm25"], row["pm10"],
                    row["temperature"], row["humidity"], row["pressure"]
                ))

        conn.commit()
        logging.info("Données insérées dans les Data Marts pour chaque ville")

    except Exception as e:
        logging.error(f"Erreur lors de l'insertion des données dans les Data Marts : {e}")
        raise

# Définition du DAG
default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'start_date': datetime(2024, 2, 1),
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
    'email_on_failure': True,  # Envoi d'email en cas d'échec
    'email_on_retry': True,    # Envoi d'email en cas de nouvelle tentative
    'email': ['fbleza5@gmail.com'],
}

dag = DAG(
    'air_quality_pipeline',
    default_args=default_args,
    description='Pipeline de collecte et stockage des données qualité de l’air et météo',
    schedule_interval=timedelta(hours=2),
    catchup=False
)

task_transform = PythonOperator(
    task_id='transform_data',
    python_callable=transform_data,
    dag=dag
)

task_store = PythonOperator(
    task_id='store_data',
    python_callable=store_data_warehouse,
    op_args=[task_transform.output],  # Passer le résultat de task_transform
    dag=dag
)


task_create_data_mart = PythonOperator(
    task_id='create_data_mart',
    python_callable=create_data_mart,
    dag=dag
)

task_insert_data_mart = PythonOperator(
    task_id='insert_data_mart',
    python_callable=insert_data_mart,
    op_args=[task_transform.output],  # Passer la sortie de la tâche de transformation
    dag=dag
)

task_create_data_mart >> task_transform >> task_store >> task_insert_data_mart

