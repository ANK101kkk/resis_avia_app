
import csv

import glob

import hashlib

import os

import secrets

import sqlite3

from fastapi import FastAPI, HTTPException, Header

from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import FileResponse

from pydantic import BaseModel



BASE_DIR = os.path.dirname(os.path.abspath(__file__))



app = FastAPI(title="Решись Авиа — Динамическое ценообразование")



app.add_middleware(

    CORSMiddleware,

    allow_origins=["*"],

    allow_methods=["*"],

    allow_headers=["*"],

)



# 1. Загрузка CSV-данных через встроенный модуль csv (без numpy/pandas)

with open(os.path.join(BASE_DIR, "routes.csv"), encoding="utf-8-sig") as f:

    routes_rows = list(csv.DictReader(f))



forecast_files = glob.glob(os.path.join(BASE_DIR, "*my_forecast.csv")) or [

    os.path.join(BASE_DIR, "seasonal_forecast.csv")

]

with open(forecast_files[0], encoding="utf-8-sig") as f:

    forecast_rows = list(csv.DictReader(f))



# 2. База данных SQLite

db_path = os.path.join(BASE_DIR, "users.db")

conn = sqlite3.connect(db_path, check_same_thread=False)

conn.execute("""

    CREATE TABLE IF NOT EXISTS users (

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        username TEXT UNIQUE NOT NULL,

        password_hash TEXT NOT NULL,

        token TEXT

    )

""")

conn.commit()





def hash_password(password: str) -> str:

    return hashlib.sha256(password.encode("utf-8")).hexdigest()





class AuthRequest(BaseModel):

    username: str

    password: str





class ForecastRequest(BaseModel):

    origin: str

    destination: str

    date: str

    price: float





@app.post("/api/register")

def register(data: AuthRequest):

    username = data.username.strip()

    if len(username) < 3 or len(data.password) < 4:

        raise HTTPException(status_code=400, detail="Логин от 3 символов, пароль от 4 символов")

    token = secrets.token_hex(16)

    try:

        conn.execute(

            "INSERT INTO users (username, password_hash, token) VALUES (?, ?, ?)",

            (username, hash_password(data.password), token),

        )

        conn.commit()

    except sqlite3.IntegrityError:

        raise HTTPException(status_code=400, detail="Пользователь с таким именем уже существует")

    return {"username": username, "token": token}





@app.post("/api/login")

def login(data: AuthRequest):

    username = data.username.strip()

    cur = conn.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,))

    row = cur.fetchone()

    if not row or row[1] != hash_password(data.password):

        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    token = secrets.token_hex(16)

    conn.execute("UPDATE users SET token = ? WHERE id = ?", (token, row[0]))

    conn.commit()

    return {"username": username, "token": token}





@app.get("/api/meta")

def get_meta():

    destinations = []

    for r in routes_rows:

        parts = r["name"].split("—") if "—" in r["name"] else r["name"].split("-")

        dest_city = parts[1].strip() if len(parts) > 1 else r["route"]

        destinations.append({

            "route": r["route"],

            "destination": dest_city,

            "base_price": float(r["base_price"]),

            "price_min": float(r["price_min"]),

            "price_max": float(r["price_max"]),

        })

    dates = sorted({r["date"] for r in forecast_rows})

    return {"origin": "Москва", "destinations": destinations, "dates": dates}





@app.post("/api/predict")

def predict(req: ForecastRequest, authorization: str | None = Header(default=None)):

    if not authorization:

        raise HTTPException(status_code=401, detail="Требуется авторизация")

    token = authorization.replace("Bearer ", "").strip()

    cur = conn.execute("SELECT id FROM users WHERE token = ?", (token,))

    if not cur.fetchone():

        raise HTTPException(status_code=401, detail="Сессия истекла, войдите снова")



    if req.origin.strip().lower() != "москва":

        raise HTTPException(status_code=400, detail="Вылеты доступны только из Москвы")



    matched_route = None

    for r in routes_rows:

        if req.destination.strip().lower() in r["name"].lower() or req.destination.strip().upper() == r["route"]:

            matched_route = r

            break



    if matched_route is None:

        raise HTTPException(status_code=404, detail="Маршрут не найден")



    route_code = matched_route["route"]

    base_price = float(matched_route["base_price"])

    sensitivity = float(matched_route["price_sensitivity"])



    flight = next((r for r in forecast_rows if r["route"] == route_code and r["date"] == req.date), None)

    if flight is None:

        raise HTTPException(status_code=404, detail="Рейс на выбранную дату не найден")



    seats = int(float(flight["seats"]))

    median_base_demand = float(flight["0.5_base_demand"])

    desired_median = max(0.0, median_base_demand - sensitivity * (req.price - base_price))

    sold_median = min(seats, max(0.0, desired_median))



    quantiles = ["0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9"]

    distribution = []

    for q in quantiles:

        q_base = float(flight[f"{q}_base_demand"])

        q_desired = max(0.0, q_base - sensitivity * (req.price - base_price))

        distribution.append({

            "quantile": q,

            "desired": round(q_desired, 1),

            "sold": round(min(seats, q_desired), 1),

        })



    return {

        "route": route_code,

        "route_name": matched_route["name"],

        "date": req.date,

        "price": req.price,

        "base_price": base_price,

        "seats": seats,

        "desired": round(desired_median, 1),

        "sold": round(sold_median),

        "recommended_price": int(float(flight["0.5_price"])),

        "distribution": distribution,

    }





@app.get("/")

def serve_frontend():

    return FileResponse(os.path.join(BASE_DIR, "frontend.html"))

