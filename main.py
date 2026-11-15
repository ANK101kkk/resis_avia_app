import glob
import hashlib
import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# Пути к файлам проекта
BASE_DIR = Path(__file__).resolve().parent
ROUTES_PATH = BASE_DIR / "routes.csv"
INDEX_PATH = BASE_DIR / "index.html"
DB_PATH = BASE_DIR / "users.db"

# Автоматически ищем файл прогноза (*my_forecast.csv или seasonal_forecast.csv)
forecast_candidates = sorted(BASE_DIR.glob("*my_forecast.csv"))
if forecast_candidates:
    FORECAST_PATH = forecast_candidates[0]
elif (BASE_DIR / "seasonal_forecast.csv").exists():
    FORECAST_PATH = BASE_DIR / "seasonal_forecast.csv"
else:
    FORECAST_PATH = BASE_DIR / "my_forecast.csv"


# =====================================================================
# 1. БАЗА ДАННЫХ SQLITE И БЕЗОПАСНОСТЬ
# =====================================================================

def get_db_connection() -> sqlite3.Connection:
    """Создаёт подключение к локальной базе SQLite."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаёт таблицу пользователей при запуске сервера."""
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                token TEXT
            )
            """
        )
        conn.commit()


def hash_password(password: str, salt: Optional[str] = None) -> str:
    """Хеширует пароль с солью (SHA-256) в формат 'salt$hash'."""
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def verify_password(plain_password: str, stored_password_hash: str) -> bool:
    """Проверяет соответствие введённого пароля сохранённому хешу."""
    if "$" not in stored_password_hash:
        return False
    salt, _ = stored_password_hash.split("$", maxsplit=1)
    return secrets.compare_digest(hash_password(plain_password, salt), stored_password_hash)


def get_current_user(authorization: Optional[str] = Header(default=None)) -> str:
    """Проверяет заголовок Authorization и возвращает имя авторизованного пользователя."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется авторизация: заголовок Authorization отсутствует.",
        )

    token = authorization.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Передан пустой токен авторизации.",
        )

    with get_db_connection() as conn:
        cursor = conn.execute("SELECT username FROM users WHERE token = ?", (token,))
        user = cursor.fetchone()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Недействительный или истекший токен авторизации.",
        )

    return str(user["username"])


# =====================================================================
# 2. ИНИЦИАЛИЗАЦИЯ FASTAPI И ЗАГРУЗКА CSV-ДАННЫХ
# =====================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Решись Авиа — Динамическое ценообразование",
    description="API для прогнозирования спроса и продаж авиабилетов",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    routes_df = pd.read_csv(ROUTES_PATH)
    forecast_df = pd.read_csv(FORECAST_PATH)
    forecast_df["date"] = forecast_df["date"].astype(str).str.strip()
except FileNotFoundError as exc:
    raise RuntimeError(f"Не найден обязательный CSV-файл: {exc.filename}") from exc

QUANTILES: List[str] = ["0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9"]


# =====================================================================
# 3. PYDANTIC-МОДЕЛИ
# =====================================================================

class AuthRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=4, max_length=128)


class AuthResponse(BaseModel):
    username: str
    token: str


class PredictRequest(BaseModel):
    origin: str = Field(..., examples=["Москва"])
    destination: str = Field(..., examples=["Сочи"])
    date: str = Field(..., examples=["2026-06-12"])
    price: float = Field(..., gt=0, examples=[9000])


class QuantilePoint(BaseModel):
    quantile: str
    desired: float


class PredictResponse(BaseModel):
    seats: int
    desired: float
    sold: float
    distribution: List[QuantilePoint]


class DestinationInfo(BaseModel):
    route: str
    name: str
    destination: str
    base_price: float
    price_min: float
    price_max: float


class MetaResponse(BaseModel):
    origins: List[str]
    destinations: List[DestinationInfo]
    dates: List[str]


# =====================================================================
# 4. ЭНДПОИНТЫ РЕГИСТРАЦИИ И АВТОРИЗАЦИИ
# =====================================================================

@app.post("/api/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register_user(payload: AuthRequest) -> AuthResponse:
    clean_username = payload.username.strip()
    if len(clean_username) < 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Имя пользователя должно содержать минимум 3 символа.",
        )

    password_hashed = hash_password(payload.password)
    token = secrets.token_hex(32)

    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, token) VALUES (?, ?, ?)",
                (clean_username, password_hashed, token),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Пользователь с таким именем уже зарегистрирован.",
        )

    return AuthResponse(username=clean_username, token=token)


@app.post("/api/login", response_model=AuthResponse)
def login_user(payload: AuthRequest) -> AuthResponse:
    clean_username = payload.username.strip()

    with get_db_connection() as conn:
        cursor = conn.execute(
            "SELECT id, password_hash FROM users WHERE username = ?",
            (clean_username,),
        )
        user = cursor.fetchone()

        if user is None or not verify_password(payload.password, str(user["password_hash"])):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Неверное имя пользователя или пароль.",
            )

        new_token = secrets.token_hex(32)
        conn.execute(
            "UPDATE users SET token = ? WHERE id = ?",
            (new_token, user["id"]),
        )
        conn.commit()

    return AuthResponse(username=clean_username, token=new_token)


# =====================================================================
# 5. ЭНДПОИНТЫ РАСЧЁТА СПРОСА, МЕТАДАННЫХ И ОТДАЧИ FRONTEND
# =====================================================================

def _extract_cities(route_name: str) -> tuple[str, str]:
    separator = "—" if "—" in route_name else "-"
    parts = [part.strip() for part in route_name.split(separator, maxsplit=1)]
    if len(parts) == 2:
        return parts[0], parts[1]
    return "Москва", route_name.strip()


def _calculate_desired(base_demand: float, sensitivity: float, price: float, base_price: float) -> float:
    return base_demand - sensitivity * (price - base_price)


@app.post("/api/predict", response_model=PredictResponse)
def predict_demand(
    payload: PredictRequest,
    current_user: str = Depends(get_current_user),
) -> PredictResponse:
    origin_clean = payload.origin.strip().lower()
    dest_clean = payload.destination.strip().lower()
    date_clean = payload.date.strip()

    matched_route = None
    for _, row in routes_df.iterrows():
        orig_city, dest_city = _extract_cities(str(row["name"]))
        route_code = str(row["route"]).strip().lower()

        if origin_clean in (orig_city.lower(), "mow") and (
            dest_clean == dest_city.lower() or dest_clean == route_code
        ):
            matched_route = row
            break

    if matched_route is None:
        raise HTTPException(
            status_code=404,
            detail=f"Маршрут '{payload.origin} -> {payload.destination}' не найден в справочнике.",
        )

    route_code = str(matched_route["route"]).strip()
    base_price = float(matched_route["base_price"])
    sensitivity = float(matched_route["price_sensitivity"])

    flight_rows = forecast_df[
        (forecast_df["route"].str.strip() == route_code) & (forecast_df["date"] == date_clean)
    ]

    if flight_rows.empty:
        raise HTTPException(
            status_code=404,
            detail=f"Рейс {route_code} на дату {date_clean} не найден в прогнозе.",
        )

    flight = flight_rows.iloc[0]
    seats = int(flight["seats"])

    median_base_demand = float(flight["0.5_base_demand"])
    desired_median = _calculate_desired(median_base_demand, sensitivity, payload.price, base_price)
    sold_median = min(float(seats), max(0.0, desired_median))

    distribution: List[QuantilePoint] = []
    for q in QUANTILES:
        q_demand = float(flight[f"{q}_base_demand"])
        q_desired = _calculate_desired(q_demand, sensitivity, payload.price, base_price)
        distribution.append(
            QuantilePoint(
                quantile=q,
                desired=round(max(0.0, q_desired), 2),
            )
        )

    return PredictResponse(
        seats=seats,
        desired=round(desired_median, 2),
        sold=round(sold_median, 2),
        distribution=distribution,
    )


@app.get("/api/meta", response_model=MetaResponse)
def get_metadata() -> MetaResponse:
    origins_set = set()
    destinations: List[DestinationInfo] = []

    for _, row in routes_df.iterrows():
        orig_city, dest_city = _extract_cities(str(row["name"]))
        origins_set.add(orig_city)
        destinations.append(
            DestinationInfo(
                route=str(row["route"]).strip(),
                name=str(row["name"]).strip(),
                destination=dest_city,
                base_price=float(row["base_price"]),
                price_min=float(row["price_min"]),
                price_max=float(row["price_max"]),
            )
        )

    available_dates = sorted(forecast_df["date"].dropna().unique().tolist())

    return MetaResponse(
        origins=sorted(origins_set),
        destinations=destinations,
        dates=available_dates,
    )


@app.get("/")
def serve_frontend():
    """Отдаёт главную HTML-страницу веб-приложения."""
    if not INDEX_PATH.exists():
        raise HTTPException(status_code=404, detail="Файл index.html не найден рядом с main.py")
    return FileResponse(INDEX_PATH)