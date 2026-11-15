FROM python:3.11-slim

# Отключаем буферизацию вывода Python и запись .pyc-файлов
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Сначала копируем только зависимости для эффективного кэширования слоёв Docker
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем код приложения и CSV-файлы
COPY . .

EXPOSE 80

# Запуск FastAPI на всех сетевых интерфейсах и 80-м порту
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]