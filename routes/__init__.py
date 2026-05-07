"""Register all blueprints with the Flask app."""


def register_blueprints(app):
    from routes.query import query_bp
    from routes.results import results_bp
    from routes.chat import chat_bp
    from routes.pipeline import pipeline_bp
    from routes.visualization import viz_bp
    from routes.health import health_bp

    for bp in (query_bp, results_bp, chat_bp, pipeline_bp, viz_bp, health_bp):
        app.register_blueprint(bp)
