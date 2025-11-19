import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from bson import ObjectId

from database import db, create_document, get_documents
from schemas import Rider, Driver, Ride, Location

app = FastAPI(title="Mini Ride Hailing API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Helpers
class ObjectIdStr(str):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if isinstance(v, ObjectId):
            return str(v)
        if not ObjectId.is_valid(v):
            raise ValueError("Invalid ObjectId")
        return str(v)

def serialize(doc):
    if not doc:
        return doc
    doc = dict(doc)
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    # Convert nested ObjectIds if any
    for k, v in list(doc.items()):
        if isinstance(v, ObjectId):
            doc[k] = str(v)
    return doc

@app.get("/")
def read_root():
    return {"message": "Mini Ride Hailing Backend is running"}

@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = db.name if hasattr(db, 'name') else "❌"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but error: {str(e)[:80]}"
        else:
            response["database"] = "⚠️ Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"
    return response

# Riders
@app.post("/riders")
def create_rider(rider: Rider):
    rider_id = create_document("rider", rider)
    doc = db["rider"].find_one({"_id": ObjectId(rider_id)})
    return serialize(doc)

# Drivers
class DriverLocationUpdate(BaseModel):
    location: Location
    is_available: Optional[bool] = None

@app.post("/drivers")
def create_driver(driver: Driver):
    driver_id = create_document("driver", driver)
    doc = db["driver"].find_one({"_id": ObjectId(driver_id)})
    return serialize(doc)

@app.get("/drivers/available")
def list_available_drivers(limit: int = 20):
    drivers = get_documents("driver", {"is_available": True}, limit=limit)
    return [serialize(d) for d in drivers]

@app.patch("/drivers/{driver_id}/location")
def update_driver_location(driver_id: str, payload: DriverLocationUpdate):
    if not ObjectId.is_valid(driver_id):
        raise HTTPException(status_code=400, detail="Invalid driver id")
    update = {"location": payload.location.model_dump()}
    if payload.is_available is not None:
        update["is_available"] = payload.is_available
    res = db["driver"].update_one({"_id": ObjectId(driver_id)}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Driver not found")
    doc = db["driver"].find_one({"_id": ObjectId(driver_id)})
    return serialize(doc)

# Rides
class RideRequest(BaseModel):
    rider_id: Optional[str] = None
    rider_name: Optional[str] = None
    pickup: Location
    dropoff: Location

@app.post("/rides/request")
def request_ride(data: RideRequest):
    # Ensure rider exists (create transiently if name provided)
    rider_id: Optional[str] = data.rider_id
    if not rider_id:
        if not data.rider_name:
            raise HTTPException(status_code=400, detail="Provide rider_id or rider_name")
        rider_id = create_document("rider", Rider(name=data.rider_name))
    else:
        if not ObjectId.is_valid(rider_id):
            raise HTTPException(status_code=400, detail="Invalid rider id")
        if db["rider"].count_documents({"_id": ObjectId(rider_id)}) == 0:
            raise HTTPException(status_code=404, detail="Rider not found")

    # Find any available driver (naive). If locations exist, pick closest by simple distance
    available = list(db["driver"].find({"is_available": True}))
    driver_obj = None
    if available:
        # Pick with closest by squared distance if driver has location
        def dist2(d):
            loc = d.get("location")
            if not loc:
                return float("inf")
            return (loc.get("lat", 0) - data.pickup.lat) ** 2 + (loc.get("lng", 0) - data.pickup.lng) ** 2
        available.sort(key=dist2)
        driver_obj = available[0]

    ride = Ride(
        rider_id=str(rider_id),
        driver_id=str(driver_obj["_id"]) if driver_obj else None,
        pickup=data.pickup,
        dropoff=data.dropoff,
        status="assigned" if driver_obj else "requested",
    )
    ride_id = create_document("ride", ride)

    # If assigned, mark driver unavailable
    if driver_obj:
        db["driver"].update_one({"_id": driver_obj["_id"]}, {"$set": {"is_available": False}})

    doc = db["ride"].find_one({"_id": ObjectId(ride_id)})
    return serialize(doc)

@app.get("/rides/{ride_id}")
def get_ride(ride_id: str):
    if not ObjectId.is_valid(ride_id):
        raise HTTPException(status_code=400, detail="Invalid ride id")
    doc = db["ride"].find_one({"_id": ObjectId(ride_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Ride not found")
    return serialize(doc)

class RideStatusUpdate(BaseModel):
    status: str

@app.patch("/rides/{ride_id}/status")
def update_ride_status(ride_id: str, payload: RideStatusUpdate):
    if not ObjectId.is_valid(ride_id):
        raise HTTPException(status_code=400, detail="Invalid ride id")
    allowed = {"requested", "assigned", "accepted", "in_progress", "completed", "cancelled"}
    if payload.status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid status")

    doc = db["ride"].find_one({"_id": ObjectId(ride_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Ride not found")

    # When completed or cancelled, free the driver
    if payload.status in {"completed", "cancelled"} and doc.get("driver_id"):
        try:
            db["driver"].update_one({"_id": ObjectId(doc["driver_id"])}, {"$set": {"is_available": True}})
        except Exception:
            pass

    db["ride"].update_one({"_id": ObjectId(ride_id)}, {"$set": {"status": payload.status}})
    doc = db["ride"].find_one({"_id": ObjectId(ride_id)})
    return serialize(doc)

@app.get("/rides")
def list_rides(rider_id: Optional[str] = None, driver_id: Optional[str] = None, limit: int = 50):
    filt = {}
    if rider_id and ObjectId.is_valid(rider_id):
        filt["rider_id"] = rider_id
    if driver_id and ObjectId.is_valid(driver_id):
        filt["driver_id"] = driver_id
    rides = db["ride"].find(filt).limit(limit)
    return [serialize(r) for r in rides]

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
