import os
from flask import Flask, render_template
from .database import init_db, close_db
from .routes.mortgage import mortgage_bp
from .routes.deposit import deposit_bp
from .routes.comparison import comparison_bp

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # web/


def create_app():
    app = Flask(
        __name__,
        template_folder=os.path.join(BASE_DIR, 'templates'),
        static_folder=os.path.join(BASE_DIR, 'static'),
        static_url_path='/static',
    )

    db_path = os.environ.get('DB_PATH', os.path.normpath(os.path.join(BASE_DIR, '..', 'db', 'mortgage_web.db')))
    app.config['DB_PATH'] = db_path

    init_db(db_path)
    app.teardown_appcontext(close_db)

    app.register_blueprint(mortgage_bp)
    app.register_blueprint(deposit_bp)
    app.register_blueprint(comparison_bp)

    @app.route('/')
    def index():
        return render_template('index.html')

    return app


app = create_app()
