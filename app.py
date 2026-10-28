from flask import Flask, render_template, request
import sqlite3

app = Flask(__name__)

# --- ФУНКЦИЯ ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ ---
def init_db():
    """Создает таблицу для истории, если её еще нет"""
    conn = sqlite3.connect('database.db') # Подключаемся к файлу базы данных (он создастся сам)
    cursor = conn.cursor()
    # Создаем таблицу history с колонками: id, num1, operator, num2, result
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            num1 REAL,
            operator TEXT,
            num2 REAL,
            result TEXT
        )
    ''')
    conn.commit()
    conn.close()

# Запускаем создание базы при старте сервера
init_db()


# --- ГЛАВНАЯ СТРАНИЦА И ЛОГИКА КАЛЬКУЛЯТОРА ---
@app.route('/', methods=['GET', 'POST'])
def calculator():
    result = None
    
    if request.method == 'POST':
        try:
            num1 = float(request.form.get('num1'))
            num2 = float(request.form.get('num2'))
            operator = request.form.get('operator')
            
            # Считаем результат
            if operator == '+':
                result = num1 + num2
            elif operator == '-':
                result = num1 - num2
            elif operator == '*':
                result = num1 * num2
            elif operator == '/':
                if num2 == 0:
                    result = "Ошибка: На ноль делить нельзя!"
                else:
                    result = num1 / num2
            
            # --- СОХРАНЯЕМ В БАЗУ ДАННЫХ ---
            # Сохраняем только если расчет прошел успешно (результат — число)
            if isinstance(result, (int, float)):
                conn = sqlite3.connect('database.db')
                cursor = conn.cursor()
                # Записываем вычисление в таблицу
                cursor.execute(
                    "INSERT INTO history (num1, operator, num2, result) VALUES (?, ?, ?, ?)",
                    (num1, operator, num2, result)
                )
                conn.commit()
                conn.close()
                
        except ValueError:
            result = "Ошибка: Введите корректные числа!"
            
    # --- ДОСТАЕМ ИСТОРИЮ ИЗ БАЗЫ ДАННЫХ ---
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    # Берем последние 5 записей из таблицы истории
    cursor.execute("SELECT num1, operator, num2, result FROM history ORDER BY id DESC LIMIT 5")
    history = cursor.fetchall()
    conn.close()

    # Передаем и результат, и всю историю в HTML-шаблон
    return render_template('index.html', result=result, history=history)


if __name__ == '__main__':
    app.run(debug=True)
