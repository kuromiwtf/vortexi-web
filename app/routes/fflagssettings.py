from flask import Blueprint, render_template, request, redirect, url_for, jsonify, make_response
from app.models.fflag_group import FflagGroup
from app.models.fflag_value import FflagValue
from app.models.gameservers import GameServer
from app.extensions import redis_controller, get_remote_address
import json
import base64
import os
import requests

FFlagRoute = Blueprint("fflag", __name__, url_prefix="/")

def ClearCache( GroupId : int ):
    redis_controller.delete("fflags_" + str(GroupId))
    GenerateFFlags(GroupId, True)

def GenerateFFlags( GroupId : int, BypassCache : bool = False ) -> dict:
    if not BypassCache:
        CachedFFlags = redis_controller.get("fflags_" + str(GroupId))
        if CachedFFlags is not None:
            return json.loads(CachedFFlags)

    FFlagValues = FflagValue.query.filter_by(group_id=GroupId).all()
    if FFlagValues is None:
        return jsonify({}),200
    
    FinalData = {}
    for FFlagValue in FFlagValues:
        FinalData[FFlagValue.name] = str(base64.b64decode(FFlagValue.flag_value).decode('utf-8'))

    redis_controller.set("fflags_" + str(GroupId), json.dumps(FinalData), ex = 60 * 60)
    return FinalData

@FFlagRoute.route("/Setting/QuietGet/<group>", methods=["GET"])
@FFlagRoute.route("/Setting/QuietGet/<group>/", methods=["GET"])
@FFlagRoute.route("/Setting/Get/<group>/", methods=["GET"])
def get_fflag(group):
    api_key = request.headers.get('apiKey') or request.args.get('apiKey')
    
    json_file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'files')

    if api_key == "D6925E56-BFB9-4908-AAA2-A5B1EC4B2D78":
        json_file_path = os.path.join(json_file_path, 'Client18Setting.json')
        return get_json_response(json_file_path)
    
    if group in ["RCCServiceaRWyf4zz9jy", "ClientAppSettings", "RCCServiceS2kvmA2lVpz4g9qlbDw1"]:
        json_file_path = os.path.join(json_file_path, f'{group}.json')
        return get_json_response(json_file_path)
    elif group in ["Client2014Setting", "Client2014SharedConf"]:
        json_file_path = os.path.join(json_file_path, 'Client2014SharedConf.json')
        return get_json_response(json_file_path)
    else:
        return 'Invalid request', 400

def get_json_response(file_path):
    try:
        with open(file_path, 'r') as file:
            json_data = json.load(file)
        return jsonify(json_data), 200
    except FileNotFoundError:
        return 'File not found', 404
    except json.JSONDecodeError:
        return 'Invalid JSON file', 500
  
@FFlagRoute.route("/v1/settings/application", methods=["GET"])
def fflag_application():
    application_name = request.args.get("applicationName")
    if not application_name:
        return jsonify({"error": "Missing 'applicationName' query parameter"}), 400

    base_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'files')

    file_mapping = {
        "PCDesktopClient": os.path.join(base_path, 'PCDesktopClient.json'),
        "GD5Z5gO1n0gYX1P": os.path.join(base_path, 'PCDesktopClient.json'),
        "6sxp8X2Y02aRWyf4zz9jy": os.path.join(base_path, '2020RCCService.json'),
        "6sxp8X2Y02SecureR": os.path.join(base_path, '2020RCCService.json'),
        "S2kvmA2lVpz4g9qlbDw1": os.path.join(base_path, '2020RCCService.json'),
        "RCCServiceaRWyf4zz9jy": os.path.join(base_path, '2020RCCService.json')
    }
    # 2020 rcc fix
    request_host = request.headers.get('Host', '')
    if request_host.startswith("clientsettingzcdn."):
        json_file_path = os.path.join(base_path, 'classicrcc.json')
    else:
        json_file_path = file_mapping.get(application_name)

    #json_file_path = file_mapping.get(application_name)
    if not json_file_path:
        #os.path.join(base_path, '2020RCCService.json'),
        return jsonify({"error": f"No configuration found forr '{application_name}'"}), 404

    if not os.path.exists(json_file_path):
        return jsonify({"error": "Configuration file not found"}), 404

    try:
        with open(json_file_path, 'r') as json_file:
            json_data = json.load(json_file)
        return jsonify(json_data)
    except (json.JSONDecodeError, IOError) as e:
        return jsonify({"error": "Failed to read or parse JSON file", "details": str(e)}), 500
    
@FFlagRoute.route("/v2/settings/application/<application_name>", methods=["GET"])
def fflag_application_v2(application_name):
    base_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'files')
    
    if application_name == "PCDesktopClient":
        json_file = "2022DesktopClient.json"
    elif application_name == "PCStudioApp":
        json_file = "2022StudioApp.json"
    else:
        return jsonify({"error": "Unsupported application"}), 400

    json_file_path = os.path.join(base_path, json_file)
    
    if not os.path.exists(json_file_path):
        return jsonify({"error": "Configuration file not found"}), 404

    try:
        with open(json_file_path, 'r') as f:
            fflags_data = json.load(f)
        
        return jsonify(fflags_data)
        
    except (json.JSONDecodeError, IOError) as e:
        return jsonify({
            "error": "Failed to read configuration",
            "details": str(e)
        }), 500
    
"""@FFlagRoute.route("/v1/settings/application", methods=["GET"])
def fflag_application():

    application_name = request.args.get("applicationName")
    if not application_name:
        return jsonify({"error": "Missing 'applicationName' query parameter"}), 400

    external_url = f"https://www.solario.ws/v1/settings/application?applicationName={application_name}"

    try:
        external_response = requests.get(external_url, timeout=5)
        if external_response.status_code == 200:
            return jsonify(external_response.json()), 200
        else:
            return jsonify({"error": f"External service error: {external_response.status_code}"}), external_response.status_code
    except requests.RequestException as e:
        return jsonify({"error": f"Failed to fetch data from external service: {str(e)}"}), 500   """