from flask import Flask, render_template, request

# Создаем приложение Flask
app = Flask(__name__)

# Главная страница сайта (маршрут '/')
@app.route('/', methods=['GET', 'POST'])
def calculator():
    result = None  # Переменная для результата
    
    # Если пользователь нажал кнопку отправки формы (метод POST)
    if request.method == 'POST':
        try:
            # Получаем числа и операцию из полей ввода на сайте
            num1 = float(request.form.get('num1'))
            num2 = float(request.form.get('num2'))
            operator = request.form.get('operator')
            
            # Считаем результат в зависимости от операции
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
        except ValueError:
            result = "Ошибка: Введите корректные числа!"
            
    # Передаем HTML-странице результат вычислений (если он есть)
    return render_template('index.html', result=result)

# Запускаем сервер
if __name__ == '__main__':
    app.run(debug=True)
