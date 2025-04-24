import requests
import json
import pandas as pd
from datetime import datetime
import logging
import os

# Clés API et villes
AQICN_API_KEY = "3e656615db324d7097f7ad604d1315961c20b4ec"
OPENWEATHER_API_KEY = "1b930df782fb8928244dec558ecb466c"
CITIES = ["Paris", "Lyon", "Lille", "Marseille", "Montpellier"]

# Dossier de sauvegarde
OUTPUT_DIR = "data"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def fetch_data(city):
    air_url = f"https://api.waqi.info/feed/{city}/?token={AQICN_API_KEY}"
    weather_url = f"https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={OPENWEATHER_API_KEY}&units=metric"

    air_resp = requests.get(air_url).json().get("data", {})
    weather_resp = requests.get(weather_url).json()
    return air_resp, weather_resp

def transform_data():
    all_air_quality, all_weather = [], []

    for city in CITIES:
        air_quality, weather = fetch_data(city)

        # Transformer les données air
        air_quality_df = pd.DataFrame([{
            "city": city,
            "aqi": air_quality.get("aqi"),
            "pm25": air_quality.get("iaqi", {}).get("pm25", {}).get("v"),
            "pm10": air_quality.get("iaqi", {}).get("pm10", {}).get("v")
        }])

        # Transformer les données météo
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

    # Fusionner
    merged_df = pd.merge(final_weather_df, final_air_quality_df, on='city', how='left')

    # Nettoyage
    cols_to_check = ["aqi", "pm25", "pm10", "temperature", "humidity", "pressure"]
    for col in cols_to_check:
        merged_df[col] = pd.to_numeric(merged_df[col], errors='coerce')

    before = len(merged_df)
    merged_df = merged_df.dropna(subset=cols_to_check)
    after = len(merged_df)
    if before != after:
        logging.warning(f"{before - after} lignes supprimées à cause de valeurs manquantes.")

    return merged_df

def save_to_csv(df):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{OUTPUT_DIR}/merged_data_{timestamp}.csv"
    df.to_csv(filename, index=False)
    print(f"✅ Données sauvegardées dans {filename}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("🚀 Récupération et traitement des données...")
    df = transform_data()
    save_to_csv(df)
