import requests
import pandas as pd
import numpy as np
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
from datetime import datetime, timedelta, timezone
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import StandardScaler
import logging
from sqlalchemy import DATE, FLOAT, TEXT
from sqlalchemy.sql import text

# Configuration
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
MAX_FORECAST_DAYS = 28
CITY_CONFIG = {
    'name': 'Paris',
    'lat': 48.8566,
    'lon': 2.3522,
    'timezone': 'Europe/Paris'
}

logger = logging.getLogger("air_quality_forecast")
logger.setLevel(logging.INFO)

def create_tables():
    """Crée les tables nécessaires"""
    hook = PostgresHook(postgres_conn_id="postgres_data")
    engine = hook.get_sqlalchemy_engine()
    
    with engine.connect() as conn:
        with conn.begin():
            # Table pour les prévisions météo historiques
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS weather_forecasts_history (
                    timestamp TIMESTAMPTZ,
                    city TEXT,
                    temperature FLOAT,
                    humidity FLOAT,
                    pressure FLOAT,
                    precipitation FLOAT,
                    forecast_date DATE,
                    PRIMARY KEY (timestamp, city)
                )
            """))
            
            # Table pour les prévisions AQI
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS air_quality_forecasts (
                    forecast_date DATE,
                    city TEXT,
                    predicted_aqi FLOAT,
                    confidence_min FLOAT,
                    confidence_max FLOAT,
                    model_accuracy FLOAT,
                    aqi_category TEXT,
                    aqi_color TEXT,
                    PRIMARY KEY (forecast_date, city)
                )
            """))
            
            logger.info("Tables créées avec succès")

def get_weather_forecast():
    """Récupère les prévisions météo pour les 28 prochains jours"""
    try:
        # Paramètres de l'API OpenMeteo
        params = {
            "latitude": CITY_CONFIG['lat'],
            "longitude": CITY_CONFIG['lon'],
            "hourly": [
                "temperature_2m",
                "relative_humidity_2m",
                "pressure_msl",
                "precipitation"
            ],
            "forecast_days": 16  # Maximum autorisé par l'API
        }

        logger.info(f"Récupération des prévisions météo avec les paramètres: {params}")
        response = requests.get(OPEN_METEO_URL, params=params, timeout=25)
        response.raise_for_status()
        data = response.json()

        # Création du DataFrame pour les 16 premiers jours
        df = pd.DataFrame({
            'timestamp': pd.to_datetime(data['hourly']['time']),
            'temperature': data['hourly']['temperature_2m'],
            'humidity': data['hourly']['relative_humidity_2m'],
            'pressure': data['hourly']['pressure_msl'],
            'precipitation': data['hourly']['precipitation']
        })
        
        # Récupération des 12 jours suivants
        if MAX_FORECAST_DAYS > 16:
            # Calcul de la date de début pour le deuxième appel
            start_date = df['timestamp'].max() + pd.Timedelta(days=1)
            days_to_forecast = (MAX_FORECAST_DAYS - 16)
            
            # Paramètres pour le deuxième appel
            params_next = {
                "latitude": CITY_CONFIG['lat'],
                "longitude": CITY_CONFIG['lon'],
                "hourly": [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "pressure_msl",
                    "precipitation"
                ],
                "past_days": 1,  # Pour commencer juste après la dernière date
                "forecast_days": days_to_forecast
            }
            
            logger.info(f"Récupération des prévisions météo pour la période suivante avec les paramètres: {params_next}")
            response = requests.get(OPEN_METEO_URL, params=params_next, timeout=25)
            response.raise_for_status()
            data = response.json()
            
            # Création du DataFrame pour les jours suivants
            df_next = pd.DataFrame({
                'timestamp': pd.to_datetime(data['hourly']['time']),
                'temperature': data['hourly']['temperature_2m'],
                'humidity': data['hourly']['relative_humidity_2m'],
                'pressure': data['hourly']['pressure_msl'],
                'precipitation': data['hourly']['precipitation']
            })
            
            # Filtrage pour ne garder que les dates futures
            df_next = df_next[df_next['timestamp'] > df['timestamp'].max()]
            
            # Concaténation des deux DataFrames
            df = pd.concat([df, df_next], ignore_index=True)
        
        # Conversion du timezone
        df['timestamp'] = df['timestamp'].dt.tz_localize('UTC').dt.tz_convert(CITY_CONFIG['timezone'])
        
        # Ajout des informations de ville et de date
        df['city'] = CITY_CONFIG['name']
        df['forecast_date'] = df['timestamp'].dt.date
        
        # Enregistrement dans la table d'historique
        hook = PostgresHook(postgres_conn_id="postgres_data")
        engine = hook.get_sqlalchemy_engine()
        
        # Suppression des anciennes données et insertion des nouvelles
        with engine.begin() as conn:
            # Suppression des anciennes données
            conn.execute(text(f"DELETE FROM weather_forecasts_history WHERE city = '{CITY_CONFIG['name']}'"))
            
            # Insertion des nouvelles données
            df.to_sql('weather_forecasts_history', conn, if_exists='replace', index=False)
            
        logger.info(f"Prévisions météo enregistrées: {len(df)} lignes")
        logger.info(f"Exemple de données:\n{df.head()}")
        
        return df

    except requests.exceptions.RequestException as e:
        logger.error(f"Erreur lors de l'appel à l'API OpenMeteo: {str(e)}")
        if hasattr(e.response, 'text'):
            logger.error(f"Réponse de l'API: {e.response.text}")
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"Erreur lors de la récupération des prévisions météo: {str(e)}")
        return pd.DataFrame()

def get_aqi_category(aqi):
    """Convertit l'AQI en catégorie et couleur"""
    if aqi <= 50:
        return "Bon", "#00C853"  # Vert
    elif aqi <= 100:
        return "Modéré", "#FFD600"  # Jaune
    elif aqi <= 150:
        return "Mauvais pour les groupes sensibles", "#FF9800"  # Orange
    elif aqi <= 200:
        return "Mauvais", "#F44336"  # Rouge
    elif aqi <= 300:
        return "Très mauvais", "#9C27B0"  # Violet
    else:
        return "Dangereux", "#7F0000"  # Rouge foncé

def prepare_training_data(historical_data):
    """Prépare les données d'entraînement"""
    try:
        # Ajout des features temporelles
        historical_data['hour'] = historical_data['timestamp'].dt.hour
        historical_data['day_of_week'] = historical_data['timestamp'].dt.dayofweek
        historical_data['month'] = historical_data['timestamp'].dt.month
        
        # Sélection des features pour l'entraînement
        features = ['hour', 'day_of_week', 'month', 'temperature', 'humidity', 'pressure']
        
        # Suppression des lignes avec valeurs manquantes
        cleaned_data = historical_data.dropna(subset=['aqi'] + features)
        
        logger.info(f"Données d'entraînement préparées: {len(cleaned_data)} lignes")
        return cleaned_data, features

    except Exception as e:
        logger.error(f"Erreur lors de la préparation des données: {str(e)}")
        return pd.DataFrame(), []

def train_and_predict(**context):
    """Entraîne le modèle et génère les prévisions"""
    try:
        # 1. Récupération des données historiques
        hook = PostgresHook(postgres_conn_id="postgres_data")
        engine = hook.get_sqlalchemy_engine()
        
        query = f"SELECT * FROM data_warehouse WHERE city = '{CITY_CONFIG['name']}'"
        historical = pd.read_sql(query, engine, parse_dates=['timestamp'])
        logger.info(f"Données historiques chargées: {len(historical)} lignes")

        # 2. Préparation des données d'entraînement
        training_data, features = prepare_training_data(historical)
        if training_data.empty:
            raise ValueError("Aucune donnée valide pour l'entraînement")

        # 3. Entraînement du modèle
        model = RandomForestRegressor(
            n_estimators=100,
            max_depth=10,
            random_state=42
        )
        
        # Normalisation des features
        scaler = StandardScaler()
        X_train = scaler.fit_transform(training_data[features])
        y_train = training_data['aqi']
        
        # Entraînement
        model.fit(X_train, y_train)
        
        # Évaluation du modèle
        train_score = model.score(X_train, y_train)
        logger.info(f"Score R² du modèle: {train_score:.3f}")

        # 4. Récupération des prévisions météo
        weather_forecast = get_weather_forecast()
        if weather_forecast.empty:
            raise ValueError("Aucune prévision météo disponible")

        # 5. Préparation des données de prédiction
        weather_forecast['hour'] = weather_forecast['timestamp'].dt.hour
        weather_forecast['day_of_week'] = weather_forecast['timestamp'].dt.dayofweek
        weather_forecast['month'] = weather_forecast['timestamp'].dt.month

        # 6. Prédiction
        X_pred = scaler.transform(weather_forecast[features])
        predictions = model.predict(X_pred)
        
        # Calcul des intervalles de confiance
        std_dev = np.std(predictions)
        weather_forecast['predicted_aqi'] = predictions
        weather_forecast['confidence_min'] = np.maximum(predictions - 1.96 * std_dev, 0)
        weather_forecast['confidence_max'] = np.minimum(predictions + 1.96 * std_dev, 500)

        # 7. Agrégation par jour
        daily_forecasts = weather_forecast.set_index('timestamp').resample('D').agg({
            'predicted_aqi': 'mean',
            'confidence_min': 'mean',
            'confidence_max': 'mean'
        }).reset_index()

        # 8. Préparation des données pour la visualisation
        daily_forecasts['forecast_date'] = daily_forecasts['timestamp'].dt.date
        daily_forecasts['city'] = CITY_CONFIG['name']
        daily_forecasts['model_accuracy'] = train_score
        
        # Ajout des catégories AQI et couleurs
        daily_forecasts[['aqi_category', 'aqi_color']] = daily_forecasts['predicted_aqi'].apply(
            lambda x: pd.Series(get_aqi_category(x))
        )
        
        # Arrondi des valeurs pour la visualisation
        daily_forecasts['predicted_aqi'] = daily_forecasts['predicted_aqi'].round(1)
        daily_forecasts['confidence_min'] = daily_forecasts['confidence_min'].round(1)
        daily_forecasts['confidence_max'] = daily_forecasts['confidence_max'].round(1)
        daily_forecasts['model_accuracy'] = daily_forecasts['model_accuracy'].round(3)

        # 9. Enregistrement des prévisions
        if not daily_forecasts.empty:
            daily_forecasts[['forecast_date', 'city', 'predicted_aqi', 'confidence_min', 'confidence_max', 
                            'model_accuracy', 'aqi_category', 'aqi_color']].to_sql(
                'air_quality_forecasts',
                engine,
                if_exists='append',
                index=False,
                dtype={
                    'forecast_date': DATE,
                    'city': TEXT,
                    'predicted_aqi': FLOAT,
                    'confidence_min': FLOAT,
                    'confidence_max': FLOAT,
                    'model_accuracy': FLOAT,
                    'aqi_category': TEXT,
                    'aqi_color': TEXT
                }
            )
            logger.info(f"Prévisions enregistrées pour {len(daily_forecasts)} jours")
        else:
            logger.error("Le DataFrame daily_forecasts est vide, aucune insertion effectuée.")

            logger.info(f"Prévisions enregistrées pour {len(daily_forecasts)} jours")

    except Exception as e:
        logger.error(f"Erreur lors de l'entraînement et de la prédiction: {str(e)}")
        raise

# Configuration du DAG
default_args = {
    'owner': 'airflow',
    'start_date': datetime(2024, 1, 1, tzinfo=timezone.utc),
    'retries': 2,
    'retry_delay': timedelta(hours=1)
}

dag = DAG(
    'paris_air_quality_prod',
    default_args=default_args,
    description='Prévisions AQI sur 28 jours',
    schedule_interval='0 4 * * *',  # 4h UTC
    catchup=False,
    max_active_runs=1
)

create_table_task = PythonOperator(
    task_id='create_tables',
    python_callable=create_tables,
    dag=dag
)

forecast_task = PythonOperator(
    task_id='generate_forecasts',
    python_callable=train_and_predict,
    dag=dag
)

create_table_task >> forecast_task