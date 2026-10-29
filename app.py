from flask import Flask, render_template, request
from supabase import create_client, Client

app = Flask(__name__)

# --- ПОДКЛЮЧЕНИЕ К ОБЛАКУ SUPABASE ---
# Вставь сюда данные, которые ты скопировал из панели Supabase:
SUPABASE_URL = "https://jxqhxosadlziboaxqmou.supabase.co"  # ТВОЙ URL
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imp4cWh4b3NhZGx6aWJvYXhxbW91Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTMyODAwMzUsImV4cCI6MjEwODg1NjAzNX0.H3VU5MZerTCvw19bQ3rUpPu5KhDV2wBZ_6nbPa04QbA"                       # ТВОЙ ANON КЛЮЧ

# Создаем клиента для связи с базой
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# --- ГЛАВНАЯ СТРАНИЦА И ВЫЧИСЛЕНИЯ ---
@app.route('/', methods=['GET', 'POST'])
def calculator():
    result = None
    
    if request.method == 'POST':
        try:
            num1 = float(request.form.get('num1'))
            num2 = float(request.form.get('num2'))
            operator = request.form.get('operator')
            
            # Вычисляем результат
            if operator == '+':
                result = num1 + num2
            elif operator == '-':
                result = num1 - num2
            elif operator == '*':
                result = num1 * num2
            elif operator == '/':
                if num2 == 0:
                    result = "Ошибка: Деление на 0"
                else:
                    result = num1 / num2
            
            # --- СОХРАНЯЕМ ЗАПИСЬ В ОБЛАКО SUPABASE ---
            if isinstance(result, (int, float)):
                supabase.table('history').insert({
                    'num1': num1,
                    'operator': operator,
                    'num2': num2,
                    'result': str(result)
                }).execute()
                
        except ValueError:
            result = "Ошибка ввода!"

    # --- ЗАГРУЖАЕМ ПОСЛЕДНИЕ ЗАПИСИ ИЗ ОБЛАКА ---
    # Делаем запрос к таблице 'history', берем последние 5 записей
    response = supabase.table('history').select('*').order('id', desc=True).limit(5).execute()
    records = response.data

    # Приводим данные к удобному формату для вывода в HTML
    history = [[item['num1'], item['operator'], item['num2'], item['result']] for item in records]

    return render_template('index.html', result=result, history=history)


if __name__ == '__main__':
    app.run(debug=True)
