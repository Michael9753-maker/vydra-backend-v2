from flask import Blueprint, request, jsonify

api = Blueprint("api", __name__)

@api.route("/download", methods=["POST"])
def download():
    data = request.get_json()

    if not data:
        return jsonify({"error": "No data received"}), 400

    url = data.get("url")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    return jsonify({
        "status": "success",
        "message": "Download endpoint working",
        "url_received": url
    })