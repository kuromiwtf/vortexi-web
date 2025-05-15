from flask import Blueprint, render_template, request, redirect, url_for, flash, make_response, jsonify, abort
from app.extensions import db, redis_controller, csrf, get_remote_address
import requests
import logging
import json
import gzip
import json
import math
from datetime import datetime, timedelta

from app.models.user import User
from app.models.asset import Asset
from app.models.usereconomy import UserEconomy
from app.models.gameservers import GameServer
from app.models.placeservers import PlaceServer
from app.models.placeserver_players import PlaceServerPlayer
from app.models.place import Place
from app.models.groups import Group
from app.models.game_session_log import GameSessionLog
from app.models.universe import Universe
from app.models.user_ban import UserBan

from app.services.economy import IncrementTargetBalance
from app.services.gameserver_comm import perform_post
from app.pages.home.home import InsertRecentlyPlayed
from app.enums.TransactionType import TransactionType
from app.enums.PlaceYear import PlaceYear
from app.enums.BanType import BanType
from app.util.placeinfo import ClearPlayingCountCache, GetPlayingCount
from app.util.transactions import CreateTransaction

from config import Config

config = Config()

JobReportHandler = Blueprint('jobreporthandler', __name__, url_prefix='/')

def isValidAuthorizationToken( authtoken : str) -> GameServer:
    if authtoken is None:
        return None
    RequestAddress = get_remote_address()
    GameServerObject = GameServer.query.filter_by( accessKey = authtoken, serverIP = RequestAddress ).first()
    return GameServerObject

class InvalidGZIPData(Exception):
    pass
class InvalidJSONData(Exception):
    pass

def IncrementPlaceVisits( PlaceObj : Place ):
    PlaceObj.visitcount += 1
    
    UniverseObj : Universe = Universe.query.filter_by( id = PlaceObj.parent_universe_id ).first()
    if UniverseObj is None:
        return
    UniverseObj.visit_count += 1

    db.session.commit()

def ParsePayloadData(throwException : bool = True):
    """
        Handles RCC Post Data and returns the JSON data
    """
    if request.headers.get("Content-Encoding") == "gzip":
        try:
            data = gzip.decompress(request.data)
        except Exception as e:
            raise InvalidGZIPData("Invalid gzip data")
        try:
            JSONData = json.loads(data)
        except Exception as e:
            raise InvalidJSONData("Invalid JSON data")
    else:
        JSONData = request.json
    if JSONData is None:
        raise InvalidJSONData("Invalid JSON data")
    return JSONData

def EvictPlayer( PlaceServerObject : PlaceServer, UserId : int ):
    PlaceObj : Place = Place.query.filter_by( placeid = PlaceServerObject.serverPlaceId ).first()
    MasterServer : GameServer | None = GameServer.query.filter_by(serverId=PlaceServerObject.originServerId).first()

    try:
        if PlaceObj.placeyear in [PlaceYear.Eighteen, PlaceYear.Twenty, PlaceYear.Sixteen]:
            if PlaceObj.placeyear in [PlaceYear.Eighteen, PlaceYear.Twenty]:
                ExecutionScript = f"""{{
                    "Mode": "EvictPlayer",
                    "MessageVersion": 1,
                    "Settings": {{
                        "PlayerId": {str(UserId)}
                    }}
                }}"""
            elif PlaceObj.placeyear in [PlaceYear.Sixteen]:
                ExecutionScript = f"""for _, Player in pairs(game:GetService("Players"):GetPlayers()) do if Player.UserId == {UserId} then Player:Kick("Disconnected from game, possibly due to game joined from another device") end end"""
            else:
                raise Exception("PlaceYear is not compatible")

            ExecuteScriptRequest = perform_post(
                TargetGameserver = MasterServer,
                Endpoint = "Execute",
                JSONData = {
                    "jobid": str(PlaceServerObject.serveruuid),
                    "script": ExecutionScript,
                    "arguments": []
                }
            )

            if ExecuteScriptRequest.status_code != 200:
                raise Exception(f"Unexpected Status Code: {ExecuteScriptRequest.status_code}, {ExecuteScriptRequest.content}")
        elif PlaceObj.placeyear in [PlaceYear.Fourteen]:
            redis_controller.set(f"EvictPlayerRequest:{PlaceServerObject.serveruuid}:{UserId}", 1, ex=60)
            StartTime = datetime.utcnow()

            while redis_controller.exists(f"EvictPlayerRequest:{PlaceServerObject.serveruuid}:{UserId}"):
                if (datetime.utcnow() - StartTime).total_seconds() > 50:
                    raise Exception("Failed to evict player")
        else:
            raise Exception("PlaceYear is not compatible")

    except Exception as e:
        logging.error(f"EvictPlayer failed to send request to kick player, {e}")

@JobReportHandler.before_request
def before_request():
    requesterAddress = get_remote_address()
    if requesterAddress is None:
        logging.warning(f"Request rejected: ip not found")
        return abort(404)
    gameserverObj : GameServer = GameServer.query.filter_by(serverIP=requesterAddress).first()
    if gameserverObj is None:
        logging.warning(f"No gamesever found us stupid {requesterAddress}")
        return abort(404)
    if "UserRequest" in request.headers.get( key = "accesskey", default = "" ):
        return jsonify({
            "success": False,
            "message": "Invalid request"
        }), 400

@JobReportHandler.route('/internal/gameserver/reportshutdown', methods=['POST'])
@csrf.exempt
def reportshutdown():
    try:
        JSONData = ParsePayloadData()
    except InvalidGZIPData:
        return jsonify({"status": "error", "message": "Invalid gzip data"}),400
    except InvalidJSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data"}),400
    except Exception as e:
        return jsonify({"status": "error", "message": "Unknown error occured while parsing data"}),400
    
    if "AuthToken" not in JSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data"}),400
    
    PlaceServerOwner : GameServer | None = isValidAuthorizationToken(JSONData["AuthToken"])
    if PlaceServerOwner is None:
        return jsonify({"status": "error", "message": "Invalid authorization token"}),400
    
    placeId = JSONData["PlaceId"]
    jobId = JSONData["JobId"]

    PlaceServerObject : PlaceServer = PlaceServer.query.filter_by(serverPlaceId=placeId, serveruuid=jobId).first()
    if PlaceServerObject is None:
        return jsonify({"status": "error", "message": "Invalid place server"}),400
    
    try:
        logging.info(f"CloseJob : Closing {jobId} for place [{placeId}] because server reports shutdown")
        perform_post(
            TargetGameserver = PlaceServerOwner,
            Endpoint = "CloseJob",
            JSONData = {
                "jobid": jobId,
            }
        )
    except Exception as e:
        logging.error(f"Failed to close job ( {jobId} ) for place ( {placeId} ), {e}")
    
    PlaceServerOwner : GameServer = GameServer.query.filter_by(serverId=PlaceServerObject.originServerId).first()
    PlaceServerPlayers = PlaceServerPlayer.query.filter_by(serveruuid=jobId).all()
    for PlaceServerPlayerObject in PlaceServerPlayers:
        db.session.delete(PlaceServerPlayerObject)
    db.session.delete(PlaceServerObject)
    db.session.commit()
    
    PlaceObj : Place = Place.query.filter_by(placeid=placeId).first()
    if PlaceObj is not None:
        ClearPlayingCountCache(PlaceObj)

    return jsonify({"status": "success"}),200

@JobReportHandler.route('/internal/gameserver/reportstats', methods=['POST'])
@csrf.exempt
def reportstats():
    try:
        JSONData = ParsePayloadData()
    except InvalidGZIPData:
        return jsonify({"status": "error", "message": "Invalid gzip data"}),400
    except InvalidJSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data"}),400
    except Exception as e:
        return jsonify({"status": "error", "message": "Unknown error occured while parsing data"}),400
    
    if "AuthToken" not in JSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data"}),400
    
    PlaceServerOwner = isValidAuthorizationToken(JSONData["AuthToken"])
    if PlaceServerOwner is None:
        return jsonify({"status": "error", "message": "Invalid authorization token"}),400
    
    placeId = JSONData["PlaceId"]
    jobId = JSONData["JobId"]
    serverAliveTime = JSONData["ServerAliveTime"]

    PlaceServerObject : PlaceServer = PlaceServer.query.filter_by(serverPlaceId=placeId, serveruuid=jobId).first()
    if PlaceServerObject is None:
        return jsonify({"status": "success"}),200
    PlaceServerObject.lastping = datetime.utcnow()
    PlaceServerObject.serverRunningTime = serverAliveTime
    db.session.commit()

    return jsonify({"status": "success"}),200

@JobReportHandler.route('/internal/gameserver/reportplacevalidation', methods=['POST'])
@csrf.exempt
def reportplacevalidation():
    try:
        JSONData = ParsePayloadData()
    except InvalidGZIPData:
        return jsonify({"status": "error", "message": "Invalid gzip data"}),400
    except InvalidJSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data"}),400
    except Exception as e:
        return jsonify({"status": "error", "message": "Unknown error occured while parsing data"}),400
    
    if "AuthToken" not in JSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data"}),400
    
    PlaceServerOwner = isValidAuthorizationToken(JSONData["AuthToken"])
    if PlaceServerOwner is None:
        return jsonify({"status": "error", "message": "Invalid authorization token"}),400
    
    ValidationRequestId = JSONData["ReqId"]
    LoadSuccess = JSONData["LoadSuccess"]
    ErrorMessage = None
    if "ErrorMessage" in JSONData:
        ErrorMessage = JSONData["ErrorMessage"]

    redis_controller.set(f"ValidatePlaceFileRequest:{ValidationRequestId}", json.dumps({
        "valid": LoadSuccess,
        "error": ErrorMessage
    }), ex=600)
    logging.info(f"Place validation request ( {ValidationRequestId} ) has been completed")
    return jsonify({"status": "success"}),200

def HandleUserTimePlayed( UserObj : User, Timeplayed : int, serverUUID : str = None, placeId : int = None ):
    """
        For every 40 seconds the user has played a game we give them 1 ticket
        however we limit the tickets to 100 per day and the user account must be
        more than 2 days old
    """
    if serverUUID is not None and placeId is not None:
        GameSessionLogObject : GameSessionLog = GameSessionLog.query.filter_by(serveruuid=serverUUID, place_id=placeId, user_id=UserObj.id).first()
        if GameSessionLogObject is None:
            GameSessionLogObject = GameSessionLog(
                user_id = UserObj.id,
                serveruuid = serverUUID,
                place_id = placeId,
                joined_at = datetime.utcnow() - timedelta(seconds=Timeplayed),
                left_at = datetime.utcnow()
            )
            db.session.add(GameSessionLogObject)
        else:
            GameSessionLogObject.left_at = datetime.utcnow()
        db.session.commit()

    if datetime.utcnow() - timedelta(days=2) < UserObj.created:
        return

    RawTicketsEarned = math.floor(Timeplayed / 40)
    CurrentDay = datetime.utcnow().day
    TicketsEarnedToday = redis_controller.get(f"UserTicketsEarned:{UserObj.id}:{CurrentDay}")
    if TicketsEarnedToday is None:
        TicketsEarnedToday = 0
    else:
        TicketsEarnedToday = int(TicketsEarnedToday)
    
    if TicketsEarnedToday >= 500:
        return
    
    TicketsToGive = 500 - TicketsEarnedToday
    if RawTicketsEarned > TicketsToGive:
        RawTicketsEarned = TicketsToGive
    
    if RawTicketsEarned <= 0:
        return
    
    IncrementTargetBalance(UserObj, RawTicketsEarned, 1)
    CreateTransaction(
        Reciever = UserObj,
        Sender = User.query.filter_by(id=1).first(),
        CurrencyAmount = RawTicketsEarned,
        CurrencyType = 1,
        TransactionType = TransactionType.BuildersClubStipend,
        AssetId = None,
        CustomText = f"Played game for {str(round(Timeplayed,1))} seconds"
    )
    AmountOfTicketsEarnedToday = TicketsEarnedToday + RawTicketsEarned
    redis_controller.set(f"UserTicketsEarned:{UserObj.id}:{CurrentDay}", AmountOfTicketsEarnedToday, ex=86400)

@JobReportHandler.route('/internal/gameserver/verifyplayer', methods=['POST'])
@csrf.exempt
def verifyplayer():
    try:
        JSONData = ParsePayloadData()
    except InvalidGZIPData:
        return jsonify({"status": "error", "message": "Invalid gzip data"}),400
    except InvalidJSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data"}),400
    except Exception as e:
        return jsonify({"status": "error", "message": "Unknown error occured while parsing data"}),400
    
    if "AuthToken" not in JSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data"}),400

    PlaceServerOwner = isValidAuthorizationToken(JSONData["AuthToken"])
    if PlaceServerOwner is None:
        return jsonify({"status": "error", "message": "Invalid authorization token"}),400

    jobId = JSONData["JobId"]
    PlaceServerObject : PlaceServer = PlaceServer.query.filter_by(serveruuid=jobId).first()
    if PlaceServerObject is None:
        return jsonify({"status": "error", "message": "Invalid place server"}),400
    PlaceServerObject.lastping = datetime.utcnow()
    PlaceObject : Place = Place.query.filter_by(placeid=PlaceServerObject.serverPlaceId).first()
    if PlaceObject is None:
        return jsonify({"status": "error", "message": "Invalid place"}),400
    
    UserId = JSONData["UserId"]
    UserObject : User = User.query.filter_by(id=UserId).first()
    if UserObject is None or UserObject.accountstatus != 1:
        return jsonify({"status": "error", "message": "Invalid user", "authenticated": False}), 200
    if "Username" not in JSONData or "CharacterAppearance" not in JSONData or "VerificationTicket" not in JSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data", "authenticated": False}), 200

    Username = JSONData["Username"]
    CharacterAppearance = JSONData["CharacterAppearance"]
    VerificationTicket = JSONData["VerificationTicket"]

    authKeyName = f"joinashx-auth:{str(jobId)}:{str(UserId)}:{str(PlaceObject.placeid)}:{VerificationTicket}"

    if not redis_controller.exists(authKeyName):
        return jsonify({"status": "error", "message": "Invalid join request", "authenticated": False}), 200
    
    JoinInfo = json.loads(redis_controller.get(authKeyName))
    if JoinInfo is None:
        return jsonify({"status": "error", "message": "Invalid join request", "authenticated": False}), 200
    redis_controller.delete(authKeyName)
    if JoinInfo["CharacterAppearance"] != CharacterAppearance or JoinInfo["Username"] != Username:
        return jsonify({"status": "error", "message": "Invalid join request", "authenticated": False}), 200
    return jsonify({"status": "success", "message": "Valid join request", "authenticated": True}), 200

@JobReportHandler.route('/internal/gameserver/reportplayers', methods=['POST'])
@csrf.exempt
def reportplayers():
    try:
        JSONData = ParsePayloadData()
    except InvalidGZIPData:
        return jsonify({"status": "error", "message": "Invalid gzip data"}),400
    except InvalidJSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data"}),400
    except Exception as e:
        return jsonify({"status": "error", "message": "Unknown error occured while parsing data"}),400
    
    if "AuthToken" not in JSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data"}),400

    PlaceServerOwner = isValidAuthorizationToken(JSONData["AuthToken"])
    if PlaceServerOwner is None:
        return jsonify({"status": "error", "message": "Invalid authorization token"}),400
    
    jobId = JSONData["JobId"]
    players = JSONData["Players"] # Array of players in the server which each player is a dictionary ( { "UserId": 1, "Name": "test" } )
    playerCount = len(players)

    PlaceServerObject : PlaceServer = PlaceServer.query.filter_by(serveruuid=jobId).first()
    if PlaceServerObject is None:
        return jsonify({"status": "error", "message": "Invalid place server"}),400
    PlaceServerObject.lastping = datetime.utcnow()
    PlaceObject : Place = Place.query.filter_by(placeid=PlaceServerObject.serverPlaceId).first()
    if PlaceObject is None:
        return jsonify({"status": "error", "message": "Invalid place"}),400
    AssetObject : Asset = Asset.query.filter_by(id=PlaceObject.placeid).first()
    CreatorObject : User | Group = None
    if AssetObject.creator_type == 0:
        CreatorObject = User.query.filter_by(id=AssetObject.creator_id).first()
    else:
        CreatorObject = Group.query.filter_by(id=AssetObject.creator_id).first()

    BadPlayers = [] # Array of players which must be kicked from the server
    for player in players:
        UserObject : User = User.query.filter_by(id=player["UserId"]).first()
        if UserObject is None or UserObject.username != player["Name"] or UserObject.accountstatus != 1:
            logging.info(f"/internal/gameserver/reportplayers - {jobId} - Invalid Player ( {player['UserId']} ) - {player['Name']}")
            BadPlayers.append(player["UserId"])
            playerCount -= 1
            continue

        if redis_controller.exists(f"EvictPlayerRequest:{jobId}:{player['UserId']}"):
            redis_controller.delete(f"EvictPlayerRequest:{jobId}:{player['UserId']}")
            logging.info(f"/internal/gameserver/reportplayers - {jobId} - Player ( {player['UserId']} ) requested kick")
            BadPlayers.append(player["UserId"])
            playerCount -= 1
            continue

        PlaceServerPlayerObject : PlaceServerPlayer = PlaceServerPlayer.query.filter_by(userid=player["UserId"]).first()
        if PlaceServerPlayerObject is None:
            if not redis_controller.exists(f"allow_join:{str(player['UserId'])}:{PlaceObject.placeid}:{str(jobId)}"):
                logging.info(f"/internal/gameserver/reportplayers - {jobId} - Player ( {player['UserId']} ) is not allowed to join")
                BadPlayers.append(player["UserId"])
                playerCount -= 1
                continue

            PlaceServerPlayerObject = PlaceServerPlayer(serveruuid=jobId, userid=player["UserId"], joinTime= datetime.utcnow())
            IncrementPlaceVisits(PlaceObject)
            if CreatorObject is not None:
                IncrementTargetBalance(CreatorObject, 1, 1)
            UserObject.lastonline = datetime.utcnow()
            InsertRecentlyPlayed(UserObj = UserObject, PlaceId = PlaceObject.placeid)
            db.session.add(PlaceServerPlayerObject)
        else:
            UserId = player["UserId"]
            if str(PlaceServerPlayerObject.serveruuid) == jobId:
                PlaceServerPlayerObject.lastHeartbeat = datetime.utcnow()
                UserObject.lastonline = datetime.utcnow()
            else:
                OtherPlaceServerObj = PlaceServer.query.filter_by(serveruuid=PlaceServerPlayerObj.serveruuid).first()
                if OtherPlaceServerObj is not None:
                    EvictPlayer( OtherPlaceServerObj, UserId )
                
                TotalTimePlayed = (datetime.utcnow() - PlaceServerPlayerObject.joinTime).total_seconds()
                UserObj : User = User.query.filter_by(id=PlaceServerPlayerObject.userid).first()
                HandleUserTimePlayed(UserObj, TotalTimePlayed, serverUUID=jobId, placeId=PlaceObject.placeid)
                db.session.delete(PlaceServerPlayerObj)

                PlaceServerPlayerObj = PlaceServerPlayer(serveruuid=jobId, userid=UserId, joinTime= datetime.utcnow())
                IncrementPlaceVisits(PlaceObject)
                if CreatorObject is not None:
                    IncrementTargetBalance(CreatorObject, 1, 1)
                UserObject.lastonline = datetime.utcnow()
                InsertRecentlyPlayed(UserObj = UserObject, PlaceId = PlaceObject.placeid)

    PlaceServerPlayers = PlaceServerPlayer.query.filter_by(serveruuid=jobId).all()
    for PlaceServerPlayerObject in PlaceServerPlayers:
        playerFound = False
        for player in players:
            if PlaceServerPlayerObject.userid == player["UserId"]:
                playerFound = True
                break
        if playerFound == False:
            TotalTimePlayed = (datetime.utcnow() - PlaceServerPlayerObject.joinTime).total_seconds()
            UserObj : User = User.query.filter_by(id=PlaceServerPlayerObject.userid).first()
            HandleUserTimePlayed(UserObj, TotalTimePlayed, serverUUID=jobId, placeId=PlaceObject.placeid)
            db.session.delete(PlaceServerPlayerObject)

    PlaceServerObject.playerCount = playerCount
    db.session.commit()
    ClearPlayingCountCache(PlaceObject)
    return jsonify({"status": "success", "bad": BadPlayers}),200

# 2022+ endpoint
from sqlalchemy import func
from datetime import datetime
import time

@JobReportHandler.route('/internal/gameserver/reportjobid', methods=['POST'])
@JobReportHandler.route('//internal/gameserver/reportjobid', methods=['POST'])
@csrf.exempt
def reportjobid():
    MAX_RETRIES = 3
    RETRY_DELAY = 0.5  # seconds
    
    try:
        JSONData = ParsePayloadData()
    except InvalidGZIPData:
        return jsonify({"status": "error", "message": "Invalid gzip data"}), 400
    except InvalidJSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data"}), 400
    except Exception as e:
        logging.error(f"Error parsing payload data: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": "Unknown error occurred while parsing data"}), 400
    
    if "PlaceId" not in JSONData or "StartTime" not in JSONData:
        return jsonify({"status": "error", "message": "Missing PlaceId or StartTime in request"}), 400

    place_id = JSONData["PlaceId"]
    start_time = JSONData["StartTime"]

    for attempt in range(MAX_RETRIES):
        try:
            active_servers = PlaceServer.query.filter_by(serverPlaceId=place_id)\
                .filter(PlaceServer.lastping > datetime.utcnow() - timedelta(minutes=5))\
                .count()
            logging.debug(f"Attempt {attempt + 1}: Found {active_servers} active servers for place {place_id}")

            time_diff = func.abs(func.extract('epoch', PlaceServer.created_at) - start_time)
            
            place_server = PlaceServer.query.filter_by(serverPlaceId=place_id)\
                .filter(PlaceServer.lastping > datetime.utcnow() - timedelta(minutes=5))\
                .order_by(time_diff)\
                .first()

            if not place_server:
                place_server = PlaceServer.query.filter_by(serverPlaceId=place_id)\
                    .order_by(time_diff)\
                    .first()

            if not place_server:
                logging.warning(f"Attempt {attempt + 1}: No matching server found for place_id: {place_id}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
                    continue
                return jsonify({
                    "status": "error",
                    "message": "No available server found for this place",
                    "suggested_action": "create_new_server",
                    "debug_info": {
                        "place_id": place_id,
                        "active_servers_count": active_servers,
                        "total_servers_count": PlaceServer.query.filter_by(serverPlaceId=place_id).count()
                    }
                }), 404

            game_server = GameServer.query.filter_by(serverId=place_server.originServerId).first()
            
            if not game_server:
                logging.error(f"Original GameServer not found for PlaceServer {place_server.serveruuid}")
                return jsonify({
                    "status": "error",
                    "message": "Server configuration error",
                    "debug_info": {
                        "place_server_uuid": str(place_server.serveruuid),
                        "origin_server_id": str(place_server.originServerId)
                    }
                }), 500

            logging.info(f"Found matching server {place_server.serveruuid} for place {place_id}")
            return jsonify({
                "status": "success",
                "JobId": str(place_server.serveruuid),
                "ApiKey": game_server.accessKey,
                "ServerIP": place_server.serverIP,
                "ServerPort": place_server.serverPort,
                "LastPing": place_server.lastping.isoformat() if place_server.lastping else None,
                "ServerAge": (datetime.utcnow() - place_server.created_at).total_seconds() if place_server.created_at else None
            })

        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed for place {place_id}: {str(e)}", exc_info=True)
            if attempt == MAX_RETRIES - 1:
                return jsonify({
                    "status": "error",
                    "message": "Internal server error",
                    "error_details": str(e),
                    "attempts": attempt + 1
                }), 500
            time.sleep(RETRY_DELAY)
    
@JobReportHandler.route('/internal/gameserver/reportfailure', methods=['POST'])
@csrf.exempt
def reportfailure():
    # TODO: Implement this
    return jsonify({"status": "success"}),200

# 2018+ endpoints

@JobReportHandler.route("/v2/CreateOrUpdate/", methods=['POST'])
@csrf.exempt
def CreateOrUpdate():
    try:
        JSONData = ParsePayloadData()
    except InvalidGZIPData:
        return jsonify({"status": "error", "message": "Invalid gzip data"}),400
    except InvalidJSONData:
        return jsonify({"status": "error", "message": "Invalid JSON data"}),400
    except Exception as e:
        return jsonify({"status": "error", "message": "Unknown error occured while parsing data"}),400
    
    RequestHost = request.headers.get('Host')
    if RequestHost is None:
        logging.warning(f"Request rejected: No host found")
        return abort(404)
    if not (RequestHost.startswith("gameinstancesapis.") or RequestHost.startswith("gameinstances.api.") or RequestHost.startswith("gameinstances-api.")):
        logging.warning(f"Request rejected: Invalid host domain - {RequestHost}")
        return abort(404)

    AccessKey = request.args.get( key = 'apiKey', default = None, type = str)
    JobId = request.args.get( key = 'gameId', default = None, type = str)
    if AccessKey is None or JobId is None:
        return abort(404)
    
    # Newer RCCs uses temporary access keys to report their game status and stuff
    if not redis_controller.exists(f"GameServerAccessKey:{AccessKey}:{JobId}"):
        logging.warning(f"Invalid access key ( {AccessKey} ) for server ( {JobId} )")
        return abort(404)
    
    PlaceServerObj : PlaceServer = PlaceServer.query.filter_by(serveruuid=JobId).first()
    if PlaceServerObj is None:
        originServerUUID = redis_controller.get(f"place:{JobId}:origin")
        if originServerUUID is not None:
            PlaceServerOwner : GameServer = GameServer.query.filter_by(serverId=originServerUUID).first()
            if PlaceServerOwner is not None:
                logging.info(f"CloseJob : Closing {JobId} because server does not exist in database")
                perform_post(
                    TargetGameserver = PlaceServerOwner,
                    Endpoint = "CloseJob",
                    JSONData = {
                        "jobid": JobId,
                    }
                )

        logging.warning(f"Invalid server ( {JobId} )")
        return abort(404)
    PlaceServerOwner : GameServer = GameServer.query.filter_by(serverId=PlaceServerObj.originServerId).first()
    
    PlaceObject : Place = Place.query.filter_by(placeid=PlaceServerObj.serverPlaceId).first()
    if PlaceObject is None:
        logging.warning(f"Invalid place ( {PlaceServerObj.serverPlaceId} ) for server ( {JobId} )")
        return jsonify({"status": "error", "message": "Invalid place"}),400
    AssetObject : Asset = Asset.query.filter_by(id=PlaceObject.placeid).first()
    CreatorObject : User | Group = None
    if AssetObject.creator_type == 0:
        CreatorObject = User.query.filter_by(id=AssetObject.creator_id).first()
    else:
        CreatorObject = Group.query.filter_by(id=AssetObject.creator_id).first()
    
    PlaceServerObj.lastping = datetime.utcnow()
    GameSessions : list = JSONData["GameSessions"]
    if GameSessions is None:
        return jsonify({"status": "error", "message": "Invalid JSON data"}),400
    
    for GameSession in GameSessions:
        UserId = GameSession["UserId"]
        UserObject : User = User.query.filter_by(id=UserId).first()
        if UserObject is None or UserObject.accountstatus != 1:
            EvictPlayer( PlaceServerObj, UserId )
            logging.warning(f"User ( {UserId} ) is not a valid user, on server ( {PlaceServerObj.serveruuid} )")
            continue
        
        PlaceServerPlayerObj : PlaceServerPlayer = PlaceServerPlayer.query.filter_by(serveruuid=JobId, userid=UserId).first()
        if PlaceServerPlayerObj is None:
            PlaceServerPlayerObj = PlaceServerPlayer(serveruuid=JobId, userid=UserId, joinTime=datetime.utcnow())
            db.session.add(PlaceServerPlayerObj)
            IncrementPlaceVisits(PlaceObject)
            if CreatorObject is not None:
                IncrementTargetBalance(CreatorObject, 1, 1)
            UserObject.lastonline = datetime.utcnow()
            InsertRecentlyPlayed(UserObj = UserObject, PlaceId = PlaceObject.placeid)
        else:
            if str(PlaceServerPlayerObj.serveruuid) == JobId:
                PlaceServerPlayerObj.lastHeartbeat = datetime.utcnow()
                UserObject.lastonline = datetime.utcnow()
            else:
                OtherPlaceServerObj = PlaceServer.query.filter_by(serveruuid=PlaceServerPlayerObj.serveruuid).first()
                if OtherPlaceServerObj is not None:
                    EvictPlayer( OtherPlaceServerObj, UserId )
                
                TotalTimePlayed = (datetime.utcnow() - PlaceServerPlayerObject.joinTime).total_seconds()
                UserObj : User = User.query.filter_by(id=PlaceServerPlayerObject.userid).first()
                HandleUserTimePlayed(UserObj, TotalTimePlayed, serverUUID=JobId, placeId=PlaceObject.placeid)
                db.session.delete(PlaceServerPlayerObj)

                PlaceServerPlayerObj = PlaceServerPlayer(serveruuid=JobId, userid=UserId, joinTime= datetime.utcnow())
                IncrementPlaceVisits(PlaceObject)
                if CreatorObject is not None:
                    IncrementTargetBalance(CreatorObject, 1, 1)
                UserObject.lastonline = datetime.utcnow()
                InsertRecentlyPlayed(UserObj = UserObject, PlaceId = PlaceObject.placeid)

    PlaceServerPlayers = PlaceServerPlayer.query.filter_by(serveruuid=JobId).all()
    for PlaceServerPlayerObject in PlaceServerPlayers:
        playerFound = False
        for GameSession in GameSessions:
            if PlaceServerPlayerObject.userid == GameSession["UserId"]:
                playerFound = True
                break
        if playerFound == False:
            TotalTimePlayed = (datetime.utcnow() - PlaceServerPlayerObject.joinTime).total_seconds()
            UserObj : User = User.query.filter_by(id=PlaceServerPlayerObject.userid).first()
            HandleUserTimePlayed(UserObj, TotalTimePlayed, serverUUID=JobId, placeId=PlaceObject.placeid)
            db.session.delete(PlaceServerPlayerObject)

    if PlaceServerObj.playerCount == 0 and len(GameSessions) == 0 and PlaceServerObj.serverRunningTime > 60:
        logging.info(f"CloseJob : Closing {JobId} for place [{PlaceServerObj.serverPlaceId}] because there was no players in the server for more than 60 seconds")
        perform_post(
            TargetGameserver = PlaceServerOwner,
            Endpoint = "CloseJob",
            JSONData = {
                "jobid": JobId,
            }
        )
        logging.info(f"Server ( {JobId} ) has been shutdown because there was no players in the server")
        db.session.delete(PlaceServerObj)
        db.session.commit()
        return jsonify({"status": "success"}),200

    PlaceServerObj.serverRunningTime = int(float(request.args.get('gameTime', '1', type=str))) + 1
    PlaceServerObj.playerCount = len(GameSessions)
    db.session.commit()

    ClearPlayingCountCache(PlaceObject)
    return jsonify({"status": "success"}),200

@JobReportHandler.route("/v2.0/Refresh", methods=['POST'])
@csrf.exempt
def Refresh():
    return jsonify({"status": "success"}),200 # We don't care about this endpoint since the above endpoint will handle it

@JobReportHandler.route("/v1/Close/", methods=['POST'])
@csrf.exempt
def CloseJob():
    AccessKey = request.args.get( key = 'apiKey', default = None, type = str)
    JobId = request.args.get( key = 'gameId', default = None, type = str)
    if AccessKey is None or JobId is None:
        return abort(404)
    
    if not redis_controller.exists(f"GameServerAccessKey:{AccessKey}:{JobId}"):
        logging.warning(f"Invalid access key ( {AccessKey} ) for server ( {JobId} )")
        return abort(404)

    PlaceServerObj : PlaceServer = PlaceServer.query.filter_by(serveruuid=JobId).first()
    if PlaceServerObj is not None:
        AllPlaceServerPlayers : list[PlaceServerPlayer] = PlaceServerPlayer.query.filter_by(serveruuid=JobId).all()
        for PlaceServerPlayerObj in AllPlaceServerPlayers:
            TotalTimePlayed = (datetime.utcnow() - PlaceServerPlayerObj.joinTime).total_seconds()
            UserObj : User = User.query.filter_by(id=PlaceServerPlayerObj.userid).first()
            HandleUserTimePlayed(UserObj, TotalTimePlayed, serverUUID=JobId, placeId=PlaceServerObj.serverPlaceId)
            db.session.delete(PlaceServerPlayerObj)
        db.session.delete(PlaceServerObj)
        db.session.commit()

    return jsonify({"status": "success"}),200

@JobReportHandler.route("/game/report-water-sys", methods=['GET'])
@csrf.exempt
def ReportCheatersHandler():
    RequestHost = request.headers.get('Host')
    if RequestHost is None:
        return abort(404)
    if not RequestHost.startswith("gameinstances.api."):
        return abort(404)
    
    ReportingUserId = request.args.get( key = 'UserID', default = None, type = int)
    if ReportingUserId is None:
        return abort(404)
    ReportingMessage = request.args.get( key = 'Message', default = None, type = str)
    if ReportingMessage is None:
        return abort(404)
    AccessKey = request.args.get( key = 'AccessKey', default = '', type = str )
    if "UserRequest" in AccessKey:
        return abort(404)
    
    ReportingRemoteAddress = get_remote_address()
    ReportingGameServer : GameServer = GameServer.query.filter_by(serverIP=ReportingRemoteAddress).first()

    if ReportingGameServer is None:
        return abort(404)
    
    UserObj : User = User.query.filter_by(id=ReportingUserId).first()
    if UserObj is None:
        return abort(404)
    
    BannableErrorCodes = {
        "carol": "Lua vm hooked (20)",
        "murdle": "Cheat Engine Stable Methods (0)",
        "olivia": "Debugger found (10)"
    }

    isBanned = False
    if ReportingMessage.lower() in BannableErrorCodes:
       LastestUserBanObj : UserBan = UserBan.query.filter_by(userid=UserObj.id, acknowledged = False).order_by(UserBan.id.desc()).first()
       if LastestUserBanObj is None and UserObj.accountstatus == 1:
            NewUserBanObj = UserBan(
                userid = UserObj.id,
                author_userid = 1,
                ban_type = BanType.Deleted,
                reason = "Exploiting in games is not tolerated on Vortexi",
                moderator_note = f"Automatic ban, received detection from gameserver. Error Code: {BannableErrorCodes[ReportingMessage.lower()]} / {BannableErrorCodes[ReportingMessage.lower()]}",
                expires_at = None
            )
            db.session.add(NewUserBanObj)
            UserObj.accountstatus = 3
            db.session.commit()

            isBanned = True

    try:
        requests.post(
            url = config.CHEATER_REPORTS_DISCORD_WEBHOOK,
            json = {
                "content": f"Received Cheater Report from GameServer {ReportingGameServer.serverName} ( {ReportingGameServer.serverId} ) for User {UserObj.username} ( {UserObj.id} )\n```{ReportingMessage}```\n Was the user banned? **{isBanned}**",
                "username": "Cheater Reports"
            }
        )
    except Exception as e:
        logging.error(f"jobreporthandler : ReportCheatersHandler: Failed to send cheater report to discord, {e}")

    return "OK", 200

@JobReportHandler.route("/Game/ClientPresence.ashx", methods=['GET'])
def ClientPresence(): # Does nothing
    return "OK", 200