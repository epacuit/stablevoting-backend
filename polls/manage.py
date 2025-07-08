#
# Functions to manage polls
#

#
# Functions to manage polls
#

from fastapi import BackgroundTasks, File, UploadFile
import arrow
import random
import motor.motor_asyncio
from pymongo import read_concern
import uuid
import csv
import os
from bson import ObjectId
import humanize
from func_timeout import func_timeout, FunctionTimedOut

from pref_voting.profiles_with_ties import ProfileWithTies
from pref_voting.voting_methods import split_cycle_defeat, stable_voting, split_cycle
from polls.models import CreatePoll, UpdatePoll
from polls.helpers import generate_voter_ids
from messages.helpers import participate_email
from polls.voting import is_linear, generate_columns_from_profiles, stable_voting_with_explanations_, get_splitting_numbers, generate_csv_data

# UPDATED IMPORTS - removed fastapi_mail, added new email functions
from messages.conf import SKIP_EMAILS, send_email
import certifi

# # MongoDB connection
# mongo_details = os.getenv('MONGO_DETAILS')
# print("mongo_details ", mongo_details)
# client = motor.motor_asyncio.AsyncIOMotorClient(mongo_details, tlsCAFile=certifi.where(), tls=True)
# db = client.StableVoting.Polls

# MongoDB connection
mongo_details = os.getenv('MONGODB_URI')
print("mongo_details ", mongo_details)

# Check if we're in development (local MongoDB doesn't use SSL)
if mongo_details and ('localhost' in mongo_details or '127.0.0.1' in mongo_details):
    # Local connection without SSL
    client = motor.motor_asyncio.AsyncIOMotorClient(mongo_details)
else:
    # Production connection with SSL
    client = motor.motor_asyncio.AsyncIOMotorClient(mongo_details, tlsCAFile=certifi.where(), tls=True)

data_base = os.getenv('MONGO_DB_NAME', 'StableVoting')
db = client[data_base].Polls

print(db)


async def create_poll(background_tasks: BackgroundTasks, poll_data: CreatePoll):
    """Create a poll."""
    print("creating a poll...")
    now = arrow.now()
    print(now.format('YYYY-MM-DD HH:mm'))
    voter_ids = []
    voter_email_map = {}
    if poll_data.is_private: 
        voter_ids = generate_voter_ids(len(poll_data.voter_emails))
        voter_email_map = {vid: email for vid, email in zip(voter_ids, poll_data.voter_emails)}

    owner_id = generate_voter_ids(1)[0]
    poll = {
        "title": poll_data.title,
        "description": poll_data.description,
        "candidates": poll_data.candidates,
        "is_private": poll_data.is_private,
        "voter_ids": voter_ids,
        "voter_email_map": voter_email_map,
        "owner_id": owner_id,
        "show_rankings": poll_data.show_rankings,
        "closing_datetime": poll_data.closing_datetime,
        "timezone": poll_data.timezone,
        "can_view_outcome_before_closing": poll_data.can_view_outcome_before_closing,
        "show_outcome": poll_data.show_outcome,
        "ballots": [],
        "is_completed": False,
        "result": None,
        "creation_dt": now.format('MMMM DD, YYYY @ HH:mm')
    }
    result = await db.insert_one(poll)

    if not SKIP_EMAILS:
        # UPDATED: Admin notification using new send_email
        background_tasks.add_task(
            send_email,
            to_email="stablevoting.org@gmail.com",
            subject="New Poll Created",
            html_body=f"""<p>Poll Created: https://stablevoting.org/results/{result.inserted_id}?oid={owner_id}</p>
            <p>vote: https://stablevoting.org/vote/{result.inserted_id}?oid={owner_id}</p>            
            <p>admin: https://stablevoting.org/admin/{result.inserted_id}?oid={owner_id}</p><p></p><p>{poll}</p>""",
            tag="admin-poll-created"
        )
        
        # Also send to Eric
        background_tasks.add_task(
            send_email,
            to_email="epacuit@umd.edu",
            subject="New Poll Created",
            html_body=f"""<p>Poll Created: https://stablevoting.org/results/{result.inserted_id}?oid={owner_id}</p>
            <p>vote: https://stablevoting.org/vote/{result.inserted_id}?oid={owner_id}</p>            
            <p>admin: https://stablevoting.org/admin/{result.inserted_id}?oid={owner_id}</p><p></p><p>{poll}</p>""",
            tag="admin-poll-created"
        )

        # UPDATED: Send voter invitations using new send_email
        for em, voter_id in zip(poll_data.voter_emails, voter_ids):
            print("sending email to ", em)
            link = f"https://stablevoting.org/vote/{result.inserted_id}?vid={voter_id}"
            print(participate_email(poll_data.title, poll_data.description, link))
            
            background_tasks.add_task(
                send_email,
                to_email=em,
                subject=f"Participate in the poll: {poll_data.title}",
                html_body=participate_email(poll_data.title, poll_data.description, link),
                tag="voter-invitation"
            )

    return {"id": str(result.inserted_id), "owner_id": owner_id}


async def update_poll(id, owner_id, poll_data: UpdatePoll, background_tasks: BackgroundTasks):
    """Update a poll. """

    print("updating poll ", id)
    document = await db.find_one({"_id": ObjectId(id)}) 
    poll_data = poll_data.dict() 
    if document is None: # poll not found
        return {"error": "Poll not found."}
    else: 
        if owner_id != document["owner_id"]: 
            return {"error": "You do not have permission to modify the poll."}
        
        get_data = lambda field : poll_data[field] if poll_data[field] is not None else  document[field]

        new_voter_ids = []
        print("new_voter_emails", poll_data["new_voter_emails"])
        print("document is_private", document.get("is_private"))
        print("poll_data is_private", poll_data.get("is_private"))

        existing_voter_email_map = document.get("voter_email_map", {})
        updated_voter_email_map = existing_voter_email_map.copy()
        
        # Use the actual poll's privacy status, not the potentially None value from poll_data
        poll_is_private = get_data("is_private")
        print("poll_is_private (resolved):", poll_is_private)
        
        if poll_is_private and poll_data["new_voter_emails"] is not None and len(poll_data["new_voter_emails"]) > 0:
            print("HERE!!!") 
            new_voter_ids = generate_voter_ids(len(poll_data["new_voter_emails"]))                
            print("new voter ids ", new_voter_ids)
            new_voter_email_map = {vid: email for vid, email in zip(new_voter_ids, poll_data["new_voter_emails"])}

            # Get existing map and merge:
            existing_voter_email_map = document.get("voter_email_map", {})
            updated_voter_email_map = {**existing_voter_email_map, **new_voter_email_map}

        new_poll = {
            "title": get_data("title"),
            "description": get_data("description"),
            "is_private": get_data("is_private"),
            "voter_ids": document["voter_ids"] + new_voter_ids,
            "voter_email_map": updated_voter_email_map,
            "show_rankings": get_data("show_rankings"),
            "closing_datetime": get_data("closing_datetime") if poll_data["closing_datetime"] != "del" else None,
            "timezone": get_data("timezone"),
            "can_view_outcome_before_closing": get_data("can_view_outcome_before_closing"),
            "show_outcome": get_data("show_outcome"),
            "ballots": document["ballots"],
            "is_completed": get_data("is_completed"),
            "result": document["result"],
            "creation_dt": document["creation_dt"],
            }
        resp = {"success": "Poll updated."}
        if len(document["ballots"]) > 0: 
            new_poll["candidates"] = document["candidates"]
            if poll_data["candidates"] is not None:
                resp["message"] = "Since voters have submitted ballots, candidate names cannot be changed.   The other changes have been made to the poll." 
        else: 
            new_poll["candidates"] =  get_data("candidates") 
        
        result = await db.update_one({"_id": ObjectId(id)}, {"$set": new_poll})

        if not SKIP_EMAILS: 
            if len(new_voter_ids) > 0: 
                # UPDATED: Send emails to new voters using new send_email
                for em, voter_id in zip(poll_data["new_voter_emails"], new_voter_ids):
                    print("sending email to ", em)
                    link = f"https://stablevoting.org/vote/{id}?vid={voter_id}"

                    background_tasks.add_task(
                        send_email,
                        to_email=em,
                        subject=f'Participate in the poll: {new_poll["title"]}',
                        html_body=participate_email(new_poll["title"], new_poll["description"], link),
                        tag="voter-invitation-update"
                    )

    return resp

# All other functions remain the same - no email sending in them
async def delete_poll(id, oid):
    """Delete the poll given the owner id."""
    if len(id) != 24: 
        return {"error": "Poll not found. Invalid poll id."}

    document = await db.find_one({"_id": ObjectId(id)})

    if document is None: 
        return {"error": "Poll not found."}

    if oid != document["owner_id"]: 
        return {"error": "You do not have permission to delete this poll."}

    result = await db.delete_one( {"_id": ObjectId(id), "owner_id": oid})
    if result.deleted_count == 0: 
        return {"error": "There was a problem.  The poll was not deleted."}
    else: 
        return {"success": "Poll deleted."}


async def poll_information(id, oid): 
    print("id ", id)
    print("oid ", oid)

    if not ObjectId.is_valid(id): 
        return {"error": "Poll not found."}

    document = await db.find_one({"_id": ObjectId(id)})  
    print(document) 

    if document is None: # poll not found
        return {"error": "Poll not found."}
    
    is_owner = document["owner_id"] == oid
    
    is_closed = poll_closed( 
        document.get("closing_datetime", None), 
        document.get("timezone", None))

    resp = {
        "is_owner": is_owner,
        "title": document.get("title", "n/a"),
        "description": document.get("description", "n/a"),
        "num_ballots": len(document["ballots"]),
        "candidates": document.get("candidates", []),
        "is_private": document.get("is_private", False),
        "num_invited_voters": len(document.get("voter_ids", list())) if document.get("is_private", False) else None,
        "show_rankings": document.get("show_rankings", True),
        "closing_datetime": document.get("closing_datetime", ""),
        "timezone": document.get("timezone", ""),
        "can_view_outcome_before_closing": document.get("can_view_outcome_before_closing", True),
        "show_outcome": document.get("show_outcome", True),
        "is_closed": is_closed,
        "is_completed": document.get("is_completed", False),
        "creation_dt": document.get("creation_dt", False),
        }
    
    if is_owner and document.get("is_private", False):
        voter_email_map = document.get("voter_email_map", {})
        voter_ids = document.get("voter_ids", [])
        email_send_counts = document.get("email_send_counts", {})
        
        voter_details = []
        if voter_email_map:
            for vid in voter_ids:
                email = voter_email_map.get(vid, "Email not available")
                voter_details.append({
                    "voter_id": vid,
                    "email": email,
                    "emailsSent": email_send_counts.get(email, 1)  # Default to 1 if not tracked
                })
        else:
            # Old polls
            for vid in voter_ids:
                voter_details.append({
                    "voter_id": vid,
                    "email": "Email not available (legacy poll)",
                    "emailsSent": 0
                })
        
        resp["voter_details"] = voter_details

    return resp

async def delete_voter(poll_id: str, voter_id: str, owner_id: str):
    """Delete a voter from a private poll."""
    if not ObjectId.is_valid(poll_id):
        return {"error": "Invalid poll ID."}
    
    document = await db.find_one({"_id": ObjectId(poll_id)})
    
    if document is None:
        return {"error": "Poll not found."}
    
    if document["owner_id"] != owner_id:
        return {"error": "Not authorized."}
    
    if not document.get("is_private", False):
        return {"error": "Can only manage voters in private polls."}
    
    # Get current voter lists
    voter_ids = document.get("voter_ids", [])
    voter_email_map = document.get("voter_email_map", {})
    
    if voter_id not in voter_ids:
        return {"error": "Voter not found."}
    
    # Remove voter_id from the list
    voter_ids.remove(voter_id)
    
    # Remove from email map if present
    if voter_id in voter_email_map:
        del voter_email_map[voter_id]
    
    # Remove any ballots from this voter
    ballots = [b for b in document["ballots"] if b.get("voter_id") != voter_id]
    
    # Update the database
    result = await db.update_one(
        {"_id": ObjectId(poll_id)}, 
        {"$set": {
            "voter_ids": voter_ids,
            "voter_email_map": voter_email_map,
            "ballots": ballots
        }}
    )
    
    if result.modified_count > 0:
        return {"success": "Voter deleted."}
    else:
        return {"error": "Failed to delete voter."}    


async def regenerate_voter_link(poll_id: str, voter_id: str, owner_id: str, background_tasks: BackgroundTasks):
    """Generate a new voter ID for an existing voter."""
    if not ObjectId.is_valid(poll_id):
        return {"error": "Invalid poll ID."}
    
    document = await db.find_one({"_id": ObjectId(poll_id)})
    
    if document is None:
        return {"error": "Poll not found."}
    
    if document["owner_id"] != owner_id:
        return {"error": "Not authorized."}
    
    if not document.get("is_private", False):
        return {"error": "Can only manage voters in private polls."}
    
    voter_ids = document.get("voter_ids", [])
    voter_email_map = document.get("voter_email_map", {})
    
    if voter_id not in voter_ids:
        return {"error": "Voter not found."}
    
    # Generate new voter ID
    new_voter_id = generate_voter_ids(1)[0]
    
    # Get the email for this voter
    email = voter_email_map.get(voter_id)
    
    # Replace old ID with new ID in voter_ids list
    voter_ids[voter_ids.index(voter_id)] = new_voter_id
    
    # Update email map if email exists
    if email and voter_id in voter_email_map:
        del voter_email_map[voter_id]
        voter_email_map[new_voter_id] = email
    
    # Update any existing ballot to use the new voter_id
    ballots = document["ballots"]
    for ballot in ballots:
        if ballot.get("voter_id") == voter_id:
            ballot["voter_id"] = new_voter_id
    
    # Update the database
    result = await db.update_one(
        {"_id": ObjectId(poll_id)}, 
        {"$set": {
            "voter_ids": voter_ids,
            "voter_email_map": voter_email_map,
            "ballots": ballots
        }}
    )
    
    if result.modified_count > 0:
        # Send email with new link
        if email and not SKIP_EMAILS:
            link = f"https://stablevoting.org/vote/{poll_id}?vid={new_voter_id}"
            
            background_tasks.add_task(
                send_email,
                to_email=email,
                subject=f"New voting link for: {document['title']}",
                html_body=f"""<p>A new voting link has been generated for you.</p>
                <p>Poll: {document['title']}</p>
                <p>Your new voting link: <a href="{link}">{link}</a></p>
                <p>Your previous link has been deactivated.</p>
                <p>You can use this link to vote or update your existing vote.</p>""",
                tag="voter-link-regenerated"
            )
        
        return {
            "success": "New voter link generated.",
            "new_voter_id": new_voter_id,
            "voteUrl": f"https://stablevoting.org/vote/{poll_id}?vid={new_voter_id}"
        }
    else:
        return {"error": "Failed to generate new link."}
    
async def delete_ballot(id, vid):
    """Given a voter id, delete a ballot from the poll"""
    document = await db.find_one({"_id": ObjectId(id)})  
    if document is None: # poll not found
        return {"error": "Poll not found."}
    else: 
        ballots = document["ballots"]
        if document["is_private"] and (vid is not None and vid in document["voter_ids"]): 
            for bidx, b in enumerate(ballots): 
                if b["voter_id"] == vid: 
                    # update
                    ballots.pop(bidx)
                    await db.update_one( {"_id": ObjectId(id)}, {"$set": {"ballots": ballots}})
                    return {"success": "Ballot deleted."}
        elif document["is_private"] and (vid is  None or vid not in document["voter_ids"]): 
            return {"error": "Voter id not found, cannot delete the ballot."}
        elif not document["is_private"]: 
            return {"error": "Can only delete ballots in private polls."}
        return {"error": "Ballot not found."}


async def delete_all_ballots(id, owner_id):
    """Delete all ballots from a poll."""
    if not ObjectId.is_valid(id):
        return {"error": "Invalid poll ID."}
    
    document = await db.find_one({"_id": ObjectId(id)})
    
    if document is None:
        return {"error": "Poll not found."}
    
    if document["owner_id"] != owner_id:
        return {"error": "You do not have permission to delete ballots from this poll."}
    
    # Check if poll is closed or completed
    if document.get("is_completed", False):
        return {"error": "Cannot delete ballots from a completed poll."}
    
    if poll_closed(document.get("closing_datetime", None), document.get("timezone", None)):
        return {"error": "Cannot delete ballots from a closed poll."}
    
    # Get the number of ballots to be deleted for the response
    num_ballots = len(document.get("ballots", []))
    
    if num_ballots == 0:
        return {"error": "No ballots to delete."}
    
    # Delete all ballots
    result = await db.update_one(
        {"_id": ObjectId(id)},
        {"$set": {"ballots": []}}
    )
    
    if result.modified_count > 0:
        return {"success": f"Successfully deleted {num_ballots} ballot(s)."}
    else:
        return {"error": "Failed to delete ballots."}

async def submit_ballot(ballot, id, vid, allow_multiple_vote_pwd):
    """Submit a ballot to the poll."""

    allow_multiple_vote = allow_multiple_vote_pwd == os.getenv('ALLOW_MULTIPLE_VOTE_PWD')
    print("allow multiple vote: ", allow_multiple_vote)
    read_concern.ReadConcern('linearizable')
    document = await db.find_one({"_id": ObjectId(id)}) 
    if document is None: # poll not found
        return {"error": "Poll not found."}
    else: 
        ballots = document["ballots"]
        if document["is_private"] and (vid is not None and vid in document["voter_ids"]): 
            for bidx, b in enumerate(ballots): 
                if b["voter_id"] == vid: 
                    b = ballot.dict()
                    b["voter_id"] = vid
                    # update
                    ballots[bidx] = b
                    await db.update_one( {"_id": ObjectId(id)}, {"$set": {"ballots": ballots}})
                    return {"success": "Ballot submitted."}
        elif document["is_private"] and (vid is  None or vid not in document["voter_ids"]): 
            return {"error": "The poll is private."}
        elif not document["is_private"]: 
            if not allow_multiple_vote and ballot.ip != "n/a":
                for b in ballots: 
                    if b["ip"] == ballot.ip:
                        return {"error": "Already submitted a ballot."}
        b = ballot.dict()
        if vid is not None: 
            b["voter_id"] = vid
        ballots.append(b)
        await db.update_one( {"_id": ObjectId(id)}, {"$set": {"ballots": ballots}})
        return {"success": "Ballot submitted."}


async def delete_ballot(id, vid):
    """Given a voter id, delete a ballot from the poll"""
    document = await db.find_one({"_id": ObjectId(id)})  
    if document is None: # poll not found
        return {"error": "Poll not found."}
    else: 
        ballots = document["ballots"]
        if document["is_private"] and (vid is not None and vid in document["voter_ids"]): 
            for bidx, b in enumerate(ballots): 
                if b["voter_id"] == vid: 
                    # update
                    ballots.pop(bidx)
                    await db.update_one( {"_id": ObjectId(id)}, {"$set": {"ballots": ballots}})
                    return {"success": "Ballot deleted."}
        elif document["is_private"] and (vid is  None or vid not in document["voter_ids"]): 
            return {"error": "Voter id not found, cannot delete the ballot."}
        elif not document["is_private"]: 
            return {"error": "Can only delete ballots in private polls."}
        return {"error": "Ballot not found."}


async def add_rankings(id, owner_id, csv_file, overwrite): 
    """add rankings to a poll from a csv file."""
    document = await db.find_one({"_id": ObjectId(id)})  
    if document is None: # poll not found
        return {"error": "Poll not found."}
    else: 
        if owner_id != document["owner_id"]:
            return {"error": "Only the poll creater can add rankings to a poll."}
        else: 
            file_location = f"./tmpcsvfiles/{str(uuid.uuid4())}-{csv_file.filename}"
            print(file_location)
            new_ballots = list()
            candidates = document["candidates"]
            with open(file_location, "wb+") as file_object:
                print("Writing file....")
                file_object.write(csv_file.file.read())
            with open(file_location) as csvfile:
                ranking_reader = csv.reader(csvfile, delimiter=',')
                _cands = next(ranking_reader)
                cands = [c.strip() for c in _cands]
                num_cands = len(candidates)
                if not sorted(candidates) == sorted(cands[0:num_cands]):
                    print({"error": "The candidates in the file do not match the candidates in the poll."})
                        
                new_ballots=list()
                for rowidx, row in enumerate(ranking_reader):
                    if len([v for v in row if v.strip() != '']) == 0: 
                        continue
                    num_ballot = int(row[num_cands]) if len(row) > num_cands and row[num_cands] != '' and row[num_cands].isdigit() else 1
                    for nb in range(num_ballot): 
                        new_ballots += [{
                            "ranking": {c:int(r) for c,r in zip(cands, row[0:num_cands]) if r != ''},
                            "voter_id": f"bulk{rowidx}_{nb+1}",
                            "submission_date": None,
                            "ip": csv_file.filename
                        }] 

                print(new_ballots)
                curr_ballots = document["ballots"]
                if overwrite: 
                    await db.update_one( {"_id": ObjectId(id)}, {"$set": {"ballots": new_ballots}})
                    success_message = f"Replaced all the ballots with {len(new_ballots)} ballots in the poll: {document['title']}."
                else: 
                    await db.update_one( {"_id": ObjectId(id)}, {"$set": {"ballots": curr_ballots + new_ballots}})
                    success_message = f"Added {len(new_ballots)} ballots to the poll: {document['title']}."
                os.remove(file_location)
                return {"success": success_message}


###
#
# Voting 
#
###

def poll_closed(dt, tz): 
    if dt is not None:
        closing_dt = arrow.get(dt).to(tz)
    else:
        closing_dt = None
    return closing_dt < arrow.utcnow().to(tz) if closing_dt is not None else False


def dt_string(dt, tz):
    if dt is not None:
        closing_dt = arrow.get(dt).to(tz)
    else:
        closing_dt = None
    return closing_dt.format('MMMM D YYYY @ HH:mm A (ZZZ)') if closing_dt is not None else "N/A"

    
def can_view_outcome(dt, tz, is_completed, can_view_outcome_before_closing, show_outcome, is_owner, is_voter):
    '''
    An outcome can be viewed when either
    1. the person is an owner, or
    2. the person is a voter and the owner enabled show outcome and either the poll is closed or completed or the viewing the outcome before the poll closes is enabled. 
    '''
    if dt is not None:
        closing_dt = arrow.get(dt).to(tz)
    else:
        closing_dt = None
    is_closed =  closing_dt < arrow.utcnow().to(tz) if closing_dt is not None else False

    return is_owner or (is_voter and show_outcome and (closing_dt is None or is_closed or is_completed or (not is_closed and can_view_outcome_before_closing)))

        
def can_vote(vid, is_completed, is_private, voter_ids, dt, tz):
    return not is_completed and not poll_closed(dt, tz) and (not is_private or (vid in voter_ids))

    
def voter_type(poll_data, vid, oid = None): 

    is_owner = oid == poll_data.get("owner_id", False)

    is_voter = not poll_data.get("is_private", False) or (poll_data.get("is_private", False) and vid in poll_data.get("voter_ids", []))

    return is_voter, is_owner


def close_poll(id, document, result): 
    """Close the poll."""


def open_poll(id, document, result): 
    """Open the poll."""


async def poll_ranking_information(id, vid, allowmultiplevote): 
    read_concern.ReadConcern('linearizable')
    print("id ", id)

    allow_multiple_vote = allowmultiplevote == os.getenv('ALLOW_MULTIPLE_VOTE_PWD')

    if not ObjectId.is_valid(id): 
        return {
            "error": "Poll not found.",
            "poll_found": False,
            "title": "N/A",
            "allow_multiple_vote": allow_multiple_vote,
            "closing_datetime": "n/a",
            "timezone": "n/a",
            "is_closed": True,
            "is_completed": True,
            "can_vote": False,
            "can_view_outcome": False
            }


    document = await db.find_one({"_id": ObjectId(id)})  
    print(document) 

    allow_multiple_vote = allowmultiplevote == os.getenv('ALLOW_MULTIPLE_VOTE_PWD')

    if document is None: # poll not found
        return {
            "error": "Poll not found.",
            "poll_found": False,
            "title": "N/A",
            "allow_multiple_vote": allow_multiple_vote,
            "closing_datetime": "n/a",
            "timezone": "n/a",
            "is_closed": True,
            "is_completed": True,
            "can_vote": False,
            "can_view_outcome": False
            }

    # vid could either be a voter id or the owner id
    is_voter, is_owner = voter_type(document, vid, vid)
    
    is_closed = poll_closed( 
                document.get("closing_datetime", None), 
                document.get("timezone", None))
    
    is_completed = document.get("is_completed", False) or is_closed
    is_private = document.get("is_private", False)
    
    closing_dt = document.get("closing_datetime", None)
    tz = document.get("timezone", None)
    if closing_dt is not None:
        dt = arrow.get(closing_dt).to(tz)
        now = arrow.utcnow().to(tz)
        time_remaining_str = f'The poll closes in {humanize.precisedelta(dt - now, suppress=["seconds"], minimum_unit="minutes")}'
    else:
        time_remaining_str = None
    print(time_remaining_str)
    v_can_vote = can_vote(
                vid,
                is_completed,
                is_private,
                document.get("voter_ids",[]),
                closing_dt, 
                tz)
    
    v_can_view_outcome = can_view_outcome(
                closing_dt, 
                tz, 
                is_completed,
                document.get("can_view_outcome_before_closing", False), 
                document.get("show_outcome", False),
                is_owner,
                is_voter)
    
    resp = {
        "title": document["title"],
        "description": document["description"],
        "candidates": document["candidates"],
        "allow_multiple_vote": allow_multiple_vote,
        "is_private": document["is_private"],
        "ranking": {},
        "closing_datetime_str": dt_string(document.get("closing_datetime", None), document.get("timezone", None)),
        "timezone": document.get("timezone", "n/a"),
        "time_remaining_str": time_remaining_str,
        "is_closed": is_closed,
        "is_completed": is_completed,
        "can_vote": v_can_vote,
        "can_view_outcome": v_can_view_outcome
        }
    if document["is_private"] and (vid is not None and vid in document["voter_ids"]): 
        for b in document["ballots"]: 
            if b["voter_id"] == vid: 
                resp["ranking"] = b["ranking"]
    return resp


async def submitted_ranking_information(id, owner_id):

    print("Ranking information for ", id)
    print("Owner id ", owner_id)
    if len(id) != 24: 
        print("Invalid id.")
        return {"error": "Poll not found."}
    
    document = await db.find_one({"_id": ObjectId(id)}) 
    
    if document is None: # poll not found
        print("Poll not found.")
        return {"error": "Poll not found."}
    else: 
        is_voter, is_owner = voter_type(document, owner_id, owner_id)

        if not is_owner: 
            print("Not a owner.")
            return {"error": "You must be the owner to view the ranking data."}
        
        unranked_candidates = [c for c in document["candidates"] if all([c not in b["ranking"].keys() for b in document["ballots"]])]
        
        num_empty_ballots = len([b for b in document["ballots"] if all([c not in b["ranking"].keys() for c in document["candidates"]])])
        cand_to_cidx = {c: str(i) for i, c in enumerate(document["candidates"])}
        cmap = {str(cidx):c for c,cidx in cand_to_cidx.items()}

        resp = {
            "unranked_candidates": unranked_candidates,
            "num_empty_ballots": num_empty_ballots, 
            "num_voters": 0,
            "num_rows": 0,
            "columns": [[]],
            "csv_data": [[]],
            "cmap": cmap,
        }

        if len(document["ballots"]) > 0:

            prof = ProfileWithTies([{cand_to_cidx[c]: rank 
                                     for c,rank in r["ranking"].items()} 
                                     for r in document["ballots"]])
            prof.display()
            num_voters = prof.num_voters
            print(num_voters)
            columns, num_rows = generate_columns_from_profiles(prof)
            resp["num_voters"] = num_voters
            resp["num_rows"] = num_rows
            resp["columns"] = columns
            resp["csv_data"] = generate_csv_data(prof, cmap)
    return resp


async def poll_outcome(id, owner_id, voter_id):
    print("Generating poll outcome for ", id)
    print("Owner id ", owner_id)
    print("Voter id ", voter_id)
    if not ObjectId.is_valid(id): 
        return {"error": "Poll not found."}
    print("Getting document...")
    document = await db.find_one({"_id": ObjectId(id)}) 
    print("got document")
    print(document)
    error_message = ''
    if document is None: # poll not found
        print("Poll not found.")
        return {"error": "Poll not found."}
    else: 
        cand_to_cidx = {c: str(i) for i, c in enumerate(document["candidates"])}
        cmap = {str(cidx):c for c,cidx in cand_to_cidx.items()}

        is_voter, is_owner = voter_type(document, voter_id, owner_id)

        can_view = can_view_outcome(
                document.get("closing_datetime", None), 
                document.get("timezone", None), 
                document.get("is_completed", None), 
                document.get("can_view_outcome_before_closing", False), 
                document.get("show_outcome", True), 
                is_owner,
                is_voter)
        
        print("is_completed", document.get("is_completed", None))
        print("can_view ", can_view)
        title = str(document["title"])
        print("title", title)
        print(document)
        closing_datetime =  dt_string(document.get("closing_datetime", None), document.get("timezone", None))
        timezone = document["timezone"] if document["timezone"] is not None else "N/A"
        is_closed = poll_closed(document.get("closing_datetime", None), document.get("timezone", None))
        print("is closed", is_closed)
        print(document.get("is_completed", False))
        print(document.get("result", None))
        print(document.get("is_completed", False) and document.get("result", None) is not None)

        if document.get("is_completed", False) and document.get("result", None) is not None: 
            """The poll is completed and there is a saved result."""
            result = document["result"]
        else: # otherwise generate the result.    
            show_rankings = document["show_rankings"] 
            margins= {}
            num_voters = 0
            sv_winners = []
            sc_winners = []
            condorcet_winner = "N/A"
            defeat_relation = {}
            explanations = {}
            prof_is_linear = False
            linear_order = []
            splitting_numbers = {}
            num_rows = 0
            columns = [[]]
            if can_view and len(document["ballots"]) > 0:

                prof = ProfileWithTies([{cand_to_cidx[c]: rank 
                                         for c,rank in r["ranking"].items()} 
                                         for r in document["ballots"]])
                prof.display()

                if not any([len(list(r.rmap.keys())) > 0 for r in prof.rankings]):
                    error_message = "No candidates are ranked."
                else: 
                    margins = {c1: {c2: prof.margin(c1, c2) for c2 in prof.candidates} for c1 in prof.candidates}
                    condorcet_winner = prof.condorcet_winner()

                    try:
                        sc_defeat = func_timeout(2, split_cycle_defeat, args=(prof,), kwargs=None)
                        sc_winners = [str(c) for c in prof.candidates if not any([c2 for c2 in prof.candidates if sc_defeat.has_edge(c2,c)])]
                        defeat_relation = {str(c): {str(c2): sc_defeat.has_edge(c,c2) for c2 in prof.candidates} for c in prof.candidates }
                    except FunctionTimedOut:
                        sc_defeat = dict()
                        sc_winners = split_cycle(prof)
                        defeat_relation = {str(c): {} for c in prof.candidates }

                    try:
                        sv_winners, _, explanations = func_timeout(2, stable_voting_with_explanations_, args=(prof,), kwargs = {"curr_cands": None, "mem_sv_winners": {}, "explanations": {}})
                    except FunctionTimedOut:
                        sv_winners = stable_voting(prof)
                        explanations = dict()

                    num_voters = prof.num_voters
                    prof_is_linear, linear_order = is_linear(prof)
                    columns, num_rows = generate_columns_from_profiles(prof)
                    if condorcet_winner is None: 
                        try:
                            splitting_numbers = func_timeout(2, get_splitting_numbers, args=(prof,), kwargs=None)
                        except FunctionTimedOut:
                            splitting_numbers = {}
                    else: 
                        splitting_numbers = {}

            result = {
                "margins": margins, 
                "num_voters": str(num_voters),
                "cmap": cmap,
                "show_rankings": show_rankings, 
                "sv_winners": sv_winners, 
                "sc_winners": sc_winners, 
                "selected_sv_winner": None, # only set if the poll is completed
                "condorcet_winner": condorcet_winner, 
                "explanations": explanations,
                "defeats": defeat_relation,
                "splitting_numbers": splitting_numbers,
                "prof_is_linear": prof_is_linear,
                "linear_order": linear_order if prof_is_linear else [],
                "num_rows": num_rows,
                "columns":columns,
                }

            print("HELLO")
            if is_closed or document.get("is_completed", False): 
                # close the poll
                if len(sv_winners) > 1: 
                    selected_sv_winner = random.choice(sv_winners)
                    result["selected_sv_winner"] = selected_sv_winner

                print("RESULT")
                print(result)
                await db.update_one( {"_id": ObjectId(id)}, {"$set": {"result": result, "is_completed": True}})

            if not document.get("is_completed", False): 
                # remove the saved result
                await db.update_one( {"_id": ObjectId(id)}, {"$set": {"result": None}})

    result["title"] = title
    result["is_closed"] = is_closed 
    result["is_completed"] = document['is_completed']
    result["can_view"] = can_view
    result["closing_datetime"] = closing_datetime
    result["timezone"] = timezone
    result["election_id"] = str(id)
    
    if error_message != '':
        result["error"] = error_message
    return result


async def demo_poll_outcome(rankings):

    print("Generating poll outcome for ", rankings)

    closing_datetime = None
    timezone = "N/A"
    is_closed = False
    show_rankings = True 
    margins= {}
    num_voters = 0

    ballots = list()
    for r in rankings: 
        print(r)
        for n in range(int(r["num"])): 
            ballots.append(r["ranking"])
    _prof = ProfileWithTies([r for r in ballots])
    _prof.display()
    curr_rankings, counts = _prof.rankings_as_dicts_counts
    cand_to_cindex = {str(c): i for i,c in enumerate(_prof.candidates)}
    cmap = {cindx: str(c) for c, cindx in cand_to_cindex.items()}

    prof = ProfileWithTies([{cand_to_cindex[c]: r 
                             for c, r in rank.items()} 
                             for rank in curr_rankings], rcounts=counts)
    
    prof.display()
    print(cmap)
    num_ranked_cands = len(list(set([_c  for _r in prof.rankings for _c in _r.rmap.keys()])))
    if num_ranked_cands == 0: #not any([len(list(r.rmap.keys())) > 0 for r in prof.rankings]):
        columns, num_rows = generate_columns_from_profiles(prof)
        result = {
            "no_candidates_ranked": True,
            "margins": {}, 
            "num_voters": str(prof.num_voters),
            "show_rankings": show_rankings, 
            "sv_winners": list(), 
            "sc_winners": list(), 
            "selected_sv_winner": None, # only set if the poll is completed
            "condorcet_winner": None, 
            "explanations": {},
            "defeats": {},
            "splitting_numbers": {},
            "prof_is_linear": False,
            "linear_order": list(),
            "num_rows": num_rows,
            "columns":columns,
            "cmap": cmap,
        }
    if num_ranked_cands == 0: #not any([len(list(r.rmap.keys())) > 0 for r in prof.rankings]):
        columns, num_rows = generate_columns_from_profiles(prof)
        result = {
            "no_candidates_ranked": True,
            "margins": {}, 
            "num_voters": str(prof.num_voters),
            "show_rankings": show_rankings, 
            "sv_winners": list(), 
            "sc_winners": list(), 
            "selected_sv_winner": None, # only set if the poll is completed
            "condorcet_winner": None, 
            "explanations": {},
            "defeats": {},
            "splitting_numbers": {},
            "prof_is_linear": False,
            "linear_order": list(),
            "num_rows": num_rows,
            "columns":columns,
            "cmap": cmap,

        }
    else: 
        margins = {c1: {c2: prof.margin(c1, c2) for c2 in prof.candidates} for c1 in prof.candidates}
        condorcet_winner = prof.condorcet_winner()

        try:
            sc_defeat = func_timeout(2, split_cycle_defeat, args=(prof,), kwargs=None)
            sc_winners = [str(c) for c in prof.candidates if not any([c2 for c2 in prof.candidates if sc_defeat.has_edge(c2,c)])]
            defeat_relation = {str(c): {str(c2): sc_defeat.has_edge(c,c2) for c2 in prof.candidates} for c in prof.candidates }
        except FunctionTimedOut:
            sc_defeat = dict()
            sc_winners = split_cycle(prof)
            defeat_relation = {str(c): {} for c in prof.candidates }

        try:
            sv_winners, _, explanations = func_timeout(2, stable_voting_with_explanations_, args=(prof,), kwargs = {"curr_cands": None, "mem_sv_winners": {}, "explanations": {}})
        except FunctionTimedOut:
            sv_winners = stable_voting(prof)
            explanations = dict()

        num_voters = prof.num_voters
        prof_is_linear, linear_order = is_linear(prof)
        columns, num_rows = generate_columns_from_profiles(prof)
        if condorcet_winner is None: 
            try:
                splitting_numbers = func_timeout(2, get_splitting_numbers, args=(prof,), kwargs=None)
            except FunctionTimedOut:
                splitting_numbers = {}
                        #splitting_numbers = get_splitting_numbers(prof)
        else: 
            splitting_numbers = {}

        result = {
            "margins": margins, 
            "num_voters": str(num_voters),
            "show_rankings": show_rankings, 
            "sv_winners": sv_winners, 
            "sc_winners": sc_winners, 
            "selected_sv_winner": None, # only set if the poll is completed
            "condorcet_winner": condorcet_winner, 
            "explanations": explanations,
            "defeats": defeat_relation,
            "splitting_numbers": splitting_numbers,
            "prof_is_linear": prof_is_linear,
            "linear_order": linear_order if prof_is_linear else [],
            "num_rows": num_rows,
            "columns":columns,
            "cmap": cmap,
            }
        if len(sv_winners) > 1: 
            selected_sv_winner = random.choice(sv_winners)
            result["selected_sv_winner"] = selected_sv_winner

    if num_ranked_cands == 1: 
        result["one_ranked_candidate"] = True
    result["title"] = "Demo Poll"
    result["is_closed"] = is_closed 
    result["is_completed"] = False
    result["can_view"] = True
    result["closing_datetime"] = closing_datetime
    result["timezone"] = timezone
    result["election_id"] = "demo_poll"
    print("result ", result)
    return result


async def resend_voter_email(poll_id: str, voter_email: str, owner_id: str, background_tasks: BackgroundTasks):
    """Resend invitation email to a voter with a new voting link."""
    if not ObjectId.is_valid(poll_id):
        return {"error": "Invalid poll ID."}
    
    document = await db.find_one({"_id": ObjectId(poll_id)})
    
    if document is None:
        return {"error": "Poll not found."}
    
    if document["owner_id"] != owner_id:
        return {"error": "Not authorized."}
    
    if not document.get("is_private", False):
        return {"error": "Can only manage voters in private polls."}
    
    voter_email_map = document.get("voter_email_map", {})
    email_send_counts = document.get("email_send_counts", {})
    
    # Find the voter_id for this email
    voter_id = None
    for vid, email in voter_email_map.items():
        if email == voter_email:
            voter_id = vid
            break
    
    if not voter_id:
        return {"error": "Voter email not found."}
    
    # Generate new voter ID
    new_voter_id = generate_voter_ids(1)[0]
    
    # Get voter_ids list
    voter_ids = document.get("voter_ids", [])
    
    # Replace old ID with new ID in voter_ids list
    if voter_id in voter_ids:
        voter_ids[voter_ids.index(voter_id)] = new_voter_id
    
    # Update email map
    del voter_email_map[voter_id]
    voter_email_map[new_voter_id] = voter_email
    
    # Update any existing ballot to use the new voter_id
    ballots = document["ballots"]
    for ballot in ballots:
        if ballot.get("voter_id") == voter_id:
            ballot["voter_id"] = new_voter_id
    
    # Increment email send count
    email_send_counts[voter_email] = email_send_counts.get(voter_email, 1) + 1
    
    # Update the database
    result = await db.update_one(
        {"_id": ObjectId(poll_id)}, 
        {"$set": {
            "voter_ids": voter_ids,
            "voter_email_map": voter_email_map,
            "email_send_counts": email_send_counts,
            "ballots": ballots
        }}
    )
    
    if result.modified_count > 0:
        # Send email with new link
        if not SKIP_EMAILS:
            link = f"https://stablevoting.org/vote/{poll_id}?vid={new_voter_id}"
            
            background_tasks.add_task(
                send_email,
                to_email=voter_email,
                subject=f"Reminder: Participate in the poll - {document['title']}",
                html_body=f"""<p>This is a reminder to participate in the poll.</p>
                <p>Poll: {document['title']}</p>
                <p>Description: {document.get('description', '')}</p>
                <p>Your voting link: <a href="{link}">{link}</a></p>
                <p>Note: This new link replaces any previous links sent to you.</p>""",
                tag="voter-invitation-resend"
            )
        
        return {
            "success": f"Email resent to {voter_email}. Total emails sent: {email_send_counts[voter_email]}"
        }
    else:
        return {"error": "Failed to resend email."}