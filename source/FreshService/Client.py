from pydantic import Field
from pydantic_settings import BaseSettings
import markdown.htmlparser
import markdown
from requests import get, post, put, delete
import json
import re
from copy import deepcopy
import sys
from time import sleep
from os.path import exists, isfile
from FreshService.Config import Settings
from typing import List, Dict
import threading

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
import warnings

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)


TABLE_DELIMITER = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _add_missing_table_delimiters(text: str) -> str:
    """Insert the '| --- |' row under table headers that lack one, so they render as tables."""
    lines = text.splitlines()
    fixed = []
    for i, line in enumerate(lines):
        fixed.append(line)
        is_row = line.lstrip().startswith("|")
        starts_table = is_row and (i == 0 or not lines[i - 1].lstrip().startswith("|"))
        next_is_row = i + 1 < len(lines) and lines[i + 1].lstrip().startswith("|")
        if starts_table and next_is_row and not TABLE_DELIMITER.match(lines[i + 1]):
            columns = len(line.strip().strip("|").split("|"))
            fixed.append("|" + " --- |" * columns)
    return "\n".join(fixed)


def markdown_to_html(message: str) -> str:
    """Convert Markdown text, or the contents of a Markdown file path, to HTML (tables included)."""
    if exists(message) and isfile(message):
        with open(message, encoding="utf-8") as message_fh:
            message = message_fh.read()
    return markdown.markdown(_add_missing_table_delimiters(message.strip()), extensions=["tables"])


class FreshService(BaseSettings):
    settings: Settings = Settings()
    ENUM_CACHE: Dict = {"VENDOR": "./.freshservice_vendors.json",
                        "SOFTWARE": "./.freshservice_software.json"}
    FRESH_HEADER: Dict = {"Content-Type": "application/json"}
    FRESH_TEMPLATES: Dict = {}   

    VendorRegister: Dict = {}
    SoftwareRegister: Dict = {}
    
    def __load_templates(cls):
        with open(cls.settings.FRESH_TEMPLATE_FILEPATH) as templates_fh:
            cls.FRESH_TEMPLATES = json.load(templates_fh)


    def __get_paginated_api(cls, url, extract_field):
        page_number = 1
        return_data = []
        while page_number==1 or len(data[extract_field]) == 100:
            if cls.settings.VERBOSE and (page_number%5)==0:
                print(f"\tFetched {len(return_data)} {extract_field}")
                sys.stdout.write(".")
                sys.stdout.flush()
            for i in range(1, cls.settings.MAX_REQUEST_RETRIES+1):
                try:
                    resp = get(f"{url}?per_page={cls.settings.FRESH_PAGE_SIZE}&page={page_number}",
                            headers = cls.FRESH_HEADER, 
                            auth = (cls.settings.FRESH_KEY, "X"), 
                            timeout = cls.settings.MAX_REQUEST_TIMEOUT )
                    if resp.status_code == 200:
                        break
                    elif resp.status_code == 404:
                        break
                    elif resp.status_code == 429:
                        if cls.settings.VERBOSE:
                            print("HTTP-429 - Waiting:", resp.headers["retry-after"])
                        sleep_time = int(resp.headers["retry-after"])
                        sleep(sleep_time)
                    else:
                        if cls.settings.VERBOSE:
                            print(f"Failed to retrieve paginated API {i}/{cls.settings.MAX_REQUEST_RETRIES}: {resp.status_code} - {url}")
                            print(resp.content)
                        sleep(3)
                except NameResolutionError:
                    if cls.settings.VERBOSE:
                        print(f"Failed to retrieve paginated API: {url}\n{e}")
                    sleep(5)

                except Exception as e:
                    if cls.settings.VERBOSE:
                        print(f"Failed to retrieve paginated API: {url}\n{e}")
                    sleep(5)
            if resp.status_code != 200:
                if cls.settings.VERBOSE:
                    print(f"API request was not found.  Request returned HTTP-{resp.status_code}")
                    print(resp.url)
                return return_data
            else:
                data = resp.json()
                return_data.extend(data[extract_field])
            page_number += 1
        return return_data


    def __get_api(cls, url, extract_field):
        for i in range(1, cls.settings.MAX_REQUEST_RETRIES+1):
            try:
                resp = get(url, 
                           headers=cls.FRESH_HEADER, 
                           auth=(cls.settings.FRESH_KEY, "X"),
                           timeout=cls.settings.MAX_REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    return resp.json()[extract_field]
                elif resp.status_code == 404:
                    break
                elif resp.status_code == 429:
                    if cls.settings.VERBOSE:
                        print("HTTP-429 - Waiting:", resp.headers["retry-after"])
                    sleep_time = int(resp.headers["retry-after"])
                    sleep(sleep_time)
                else:
                    sleep(3)
            except Exception as e:
                print(f"Failed to retrieve single API item {i}/{cls.settings.MAX_REQUEST_RETRIES}: {url}")
                print(e)
                sleep(5)
        return {}


    def __create_new_ticket(cls, ticket_object):
        resp = post(f"https://{cls.settings.FRESH_DOMAIN}/api/v2/tickets", 
                    headers=cls.FRESH_HEADER, 
                    json=ticket_object,
                    auth=(cls.settings.FRESH_KEY, "X"))
        if resp.status_code != 201:
            print(f"Ticket was not created.\nResponse code {resp.status_code},\nmessage: {resp.json()}")
        else:
            response = resp.json()
            print(f"Ticket successfully created - {response['ticket']['id']} - {response['ticket']['subject']}")
            

    
    def __delete_software(cls, software_id:str):
        try:
            resp = delete(f"https://{cls.settings.FRESH_DOMAIN}/api/v2/applications/{software_id}",
                            headers=cls.FRESH_HEADER, auth=(cls.settings.FRESH_KEY, "X"), timeout=5)
        except TimeoutError:
            return False
        if resp.status_code == 204:
            return True
        return False


    def wipe_software(cls):
        deleted_softwares = []
        for software_id in cls.SoftwareRegister:
            count =  len(cls.SoftwareRegister[software_id]["users"])
            count += len(cls.SoftwareRegister[software_id]["installs"])
            count += len(cls.SoftwareRegister[software_id]["licenses"])
            if count > 0:
                state = False
            else:
                state = cls.__delete_software(software_id)
            if state:
                print(f"Successfully deleted: {software_id} - {cls.SoftwareRegister[software_id]['name']}")
                deleted_softwares.append(software_id)
            else:
                print(f"Failed to deleted: {software_id} with {count} relations")
        for software_id in deleted_softwares:
            software = cls.SoftwareRegister.pop(software_id)
        cls.__save_cache("SOFTWARE")


    def __load_cache(cls, CACHE_TYPE:str):
        if CACHE_TYPE not in cls.ENUM_CACHE:
            raise KeyError(f"Key '{CACHE_TYPE}' does not exist")
        print(f"Loading cache: {CACHE_TYPE}")
        try:
            with open(cls.ENUM_CACHE[CACHE_TYPE], "r") as cache_fh:
                if CACHE_TYPE == "VENDOR":
                    temp_data = json.load(cache_fh)
                elif CACHE_TYPE == "SOFTWARE":
                    temp_data = json.load(cache_fh)
                return temp_data
        except Exception as e:
            print(e)
        return {}


    def __save_cache(cls, CACHE_TYPE):
        if CACHE_TYPE not in cls.ENUM_CACHE:
            raise KeyError(f"Key '{CACHE_TYPE}' does not exist")
        print(f"Saving cache: {CACHE_TYPE}")
        try:
            with open(cls.ENUM_CACHE[CACHE_TYPE], "w") as cache_fh:
                if CACHE_TYPE == "VENDOR":
                    json.dump(cls.VendorRegister, cache_fh, indent=2)
                elif CACHE_TYPE == "SOFTWARE":
                    json.dump(cls.SoftwareRegister, cache_fh, indent=2)
                return True
        except Exception as e:
            print(e)
        return False


    def get_vendors(cls, update_cache:bool=False):
        print("Fetching: Vendors")
        if not update_cache:
            cls.VendorRegister = cls.__load_cache(CACHE_TYPE="VENDOR")
        
        if update_cache or not cls.VendorRegister:
            cls.VendorRegister.update({"UNREGISTERED": {"name": "UNREGISTERED", "software":[]}})
            vendor_list = cls.__get_paginated_api(f"https://{cls.settings.FRESH_DOMAIN}/api/v2/vendors", "vendors")
            for vendor in vendor_list:
                cls.VendorRegister.update({str(vendor["id"]): {"name": vendor["name"], "software":[]}})
            cls.__save_cache("VENDOR")


    def get_software(cls, vendor_id_list:list[str]=[], software_filter:list[str]=[], update_cache:bool=False):
        print("Fetching: Applications")
        if not update_cache:
            cls.SoftwareRegister = cls.__load_cache(CACHE_TYPE="SOFTWARE")
            
        if update_cache or not cls.SoftwareRegister:
            software_list = cls.__get_paginated_api(f"https://{cls.settings.FRESH_DOMAIN}/api/v2/applications", "applications")

            for software in software_list:
                software["id"] = str(software["id"])
                software["publisher_id"] = "UNREGISTERED" if not software["publisher_id"] else str(software["publisher_id"])
                cls.SoftwareRegister.update({software["id"]:{
                                            "name": software["name"],
                                            "publisher_id": software["publisher_id"],
                                            "category": software["category"], 
                                            "status": software["status"],
                                            "users": [],
                                            "installs": [],
                                            "licenses": []}
                                        })
            cls.expand_software(vendor_id_list=vendor_id_list, software_filter=software_filter)
            cls.__save_cache("SOFTWARE")
        elif vendor_id_list:
            cls.expand_software(vendor_id_list=vendor_id_list, software_filter=software_filter)

        for software_id, software in cls.SoftwareRegister.items():
            if software["publisher_id"] not in cls.VendorRegister:
                software["publisher_id"] = "UNREGISTERED"
            cls.VendorRegister[software["publisher_id"]]["software"].append(software_id)

    
    def __get_software_users(cls, software_id):
        if cls.settings.VERBOSE:
            print(f"Expanding application {software_id} w/users")
        data = cls.__get_paginated_api(f"https://{cls.settings.FRESH_DOMAIN}/api/v2/applications/{software_id}/users/", "application_users")
        cls.SoftwareRegister[software_id]["users"] = []
        cls.SoftwareRegister[software_id]["users"] = [{"user": app["user_id"],
                                            "license": app["license_id"],
                                            "state": app["state"], 
                                            "last_use": app["last_used"]} for app in data]


    def __get_software_licenses(cls, software_id):
        if cls.settings.VERBOSE:
            print("Expanding applications w/licenses")
        data = cls.__get_paginated_api(f"https://{cls.settings.FRESH_DOMAIN}/api/v2/applications/{software_id}/licenses", "licenses")
        cls.SoftwareRegister[software_id]["licenses"] = []
        cls.SoftwareRegister[software_id]["licenses"] = [{"license": app["id"], "contract_id": app["contract_id"]} for app in data]


    def __get_software_installs(cls, software_id):
        if cls.settings.VERBOSE:
            print("Expanding applications w/installations")
        data = cls.__get_paginated_api(f"https://{cls.settings.FRESH_DOMAIN}/api/v2/applications/{software_id}/installations/", "installations")
        cls.SoftwareRegister[software_id]["installs"] = []
        for app in data:
            asset_info = cls.__get_api(f"https://{cls.settings.FRESH_DOMAIN}/api/v2/assets/{app['installation_machine_id']}?include=type_fields", "asset")
            lcl_description = None
            if asset_info and "description" in asset_info and asset_info["description"]:
                lcl_description = BeautifulSoup(asset_info["description"], "lxml").text.replace("\n", "  |  ").strip()
            lcl_asset_info = None
            if asset_info and "asset_state_11000765764" in asset_info["type_fields"]:
                lcl_asset_info = asset_info["type_fields"]["asset_state_11000765764"]
            cls.SoftwareRegister[software_id]["installs"].append({  "path": app["installation_path"],
                                                                    "version": app["version"], 
                                                                    "user": app["user_id"],
                                                                    "name": asset_info["name"] if asset_info else None,
                                                                    "description": lcl_description,
                                                                    "status": lcl_asset_info,
                                                                    "machine": app["installation_machine_id"]
                                                                })


    def __expand_software(cls, software_id):
        cls.__get_software_users(software_id)
        cls.__get_software_licenses(software_id)
        cls.__get_software_installs(software_id)
        users = len(cls.SoftwareRegister[software_id]['users'])
        installs = len(cls.SoftwareRegister[software_id]['installs'])
        licenses = len(cls.SoftwareRegister[software_id]['licenses'])
        print(f"\tFinished expanding: {software_id} {cls.SoftwareRegister[software_id]['name']} - {users}, {installs}, {licenses}")


    def expand_software(cls, vendor_id_list:list = [], software_filter:list = []):
        print("Expanding software")
        threads = []
        for software_id, software in cls.SoftwareRegister.items():
            if vendor_id_list and software["publisher_id"] not in vendor_id_list:
                continue
            if software_filter:
                for filter in software_filter:
                    if filter in software["name"]:
                        thread = threading.Thread(target=cls.__expand_software, args=(software_id,))
                        threads.append(thread)
                        break
            else:
                thread = threading.Thread(target=cls.__expand_software, args=(software_id,))
                threads.append(thread)
        started_threads = []
        for thread in threads:
            while threading.active_count() >= 5:
                sleep(1)
            thread.start()
            started_threads.append(thread)
        for thread in threads:
            thread.join()
        cls.__save_cache("SOFTWARE")


    def list_software(cls, vendor_id_list:List[str]=[], software_id_list:List[str]=[], write:bool=False, show_usage:bool=False):
        string_builder = []
        if software_id_list and not vendor_id_list:
            for software_id in software_id_list:
                pid = cls.SoftwareRegister[software_id]['publisher_id']
                pname = cls.VendorRegister[cls.SoftwareRegister[software_id]['publisher_id']]['name']
                print(f"{pid} - {pname} - {software_id} - {cls.SoftwareRegister[software_id]['name']}")
                string_builder.append(f"- {pid} - {pname} - {software_id} - {cls.SoftwareRegister[software_id]['name']}")
        
        else:
            if not vendor_id_list:
                vendor_id_list = [key for key in cls.VendorRegister]
                print(vendor_id_list)
                vendor_id_list.sort()
            for vendor_id in vendor_id_list:
                temp_string_builder = []
                vendor = cls.VendorRegister[vendor_id]
                if not vendor['software'] and not cls.settings.VERBOSE:
                    continue
                print(f"{vendor_id} - {vendor['name']}")
                temp_string_builder.append(f"## {vendor_id} - {vendor['name']}\n")
                software_id_keys = vendor['software']
                software_id_keys.sort()
                for software_id in software_id_keys:
                    if software_id_list and software_id not in software_id_list:
                        continue
                    software = cls.SoftwareRegister[software_id]
                        
                    if not cls.settings.VERBOSE and not software["users"] and not software["installs"] and not software["licenses"]:
                        continue

                    print(f"\t{software_id} - {software['name']}")
                    CONTENT_0 = False
                    if software["installs"]:
                        version_list = list(set([install["version"] for install in software["installs"]]))
                        version_list.sort()
                        for version in version_list:
                            CONTENT_1 = False
                            for install in software["installs"]:
                                if install["version"] != version:
                                    continue
                                if not cls.settings.VERBOSE and install["status"] != "In Use":
                                    continue
                                if CONTENT_0 == False:
                                    CONTENT_0 = True
                                    temp_string_builder.append(f"### {software_id} - {software['name']}")
                                    
                                if CONTENT_1 == False:
                                    CONTENT_1 = True
                                    temp_string_builder.append(f"- v{version}")
                                if show_usage:
                                    print(f"\t\tv{version} installed on {install['name']} [User: {install['user']}] [Device state: {install['status']}]")
                                li = ""
                                if install['user']:
                                    li = f"\t- Installed on [{install['name']}](https://support.kulturit.org/cmdb/items/{install['machine']}) \\[User: [{install['user']}](https://support.kulturit.org/users/{install['user']})\\] \\[Device state: {install['status']}\\]"
                                else:
                                    li = f"\t- Installed on [{install['name']}](https://support.kulturit.org/cmdb/items/{install['machine']}) \\[User: Unknown\\] \\[Device state: {install['status']}\\]"
                                if install['description']:
                                    li += f"  \n\t\t{install['description']}"
                                    if show_usage:
                                        print(f"\t\t\t{install['description']}")
                                temp_string_builder.append(li)

                    
                    for user in software["users"]:
                        temp_string_builder.append(f"\t- User: {user['user']} [State: {user['state']}] w/{user['license']} licenses - Last used: {'N/A' if not user['last_use'] else user['last_use']}")
                        if show_usage:
                            print(f"\t\t\tUser: {user['user']} [State: {user['state']}] w/{user['license']} licenses - Last used: {'N/A' if not user['last_use'] else user['last_use']}")
                    for license in software["licenses"]:
                        temp_string_builder.append(f"\t- License: {license['license']} {license['contract_id']}")
                        if show_usage:
                            print(f"\t\t\tLicense: {license['license']} {license['contract_id']}")

                    
                if len(temp_string_builder) == 1:
                    continue
                temp_string_builder.append("")
                temp_string_builder.append("______\n")
                
                string_builder.extend(temp_string_builder)

        
        if write:
            with open("./message.md", "w") as file_handle:
                file_handle.write("\n".join(string_builder))
        return string_builder


    def filter_software(cls, filter_software:List[str]=[]):
        software_ids = []
        for key,software in cls.SoftwareRegister.items():
            for filter in filter_software:
                if filter in software["name"].lower():
                    software_ids.append(key)
                    break
        return software_ids


    def filter_vendors(cls, filter_vendor:List[str]=[]):
        vendor_ids = []
        for key,vendor in cls.VendorRegister.items():
            for filter in filter_vendor:
                if filter in vendor["name"].lower():
                    vendor_ids.append(key)
                    break
        return vendor_ids


    def list_vendors(cls, vendor_id_list:List[str]=[]):
        if vendor_id_list:
            for vendor_id in vendor_id_list:
                print(f"{vendor_id} - {cls.VendorRegister[vendor_id]['name']}")
        else:
            for key, vendor in cls.VendorRegister.items():
                print(f"{key} - {vendor['name']}")


    def list_templates(cls):
        cls.__load_templates()
        print("Defined FreshService template")
        for template_key in cls.FRESH_TEMPLATES:
            print(f"\t{template_key}: {cls.FRESH_TEMPLATES[template_key]['subject']}")
    

    def generate_ticket(cls, message: str, subject:str=None, template_name:str="DEFAULT"):
        if cls.settings.VERBOSE:
            print("Creating ticket")
        cls.__load_templates()

        if template_name not in cls.FRESH_TEMPLATES:
            print(f"Template not found: '{template_name}'")
            cls.list_templates()
            return
        
        ticket_object = deepcopy(cls.FRESH_TEMPLATES["DEFAULT"])
        ticket_object["workspace_id"] = cls.settings.FRESH_WORKSPACE_ID
        ticket_object["email"] = cls.settings.FRESH_DEFAULT_CONTACT_EMAIL
        ticket_object["cc_emails"].append(cls.settings.FRESH_DEFAULT_CONTACT_EMAIL)
        ticket_object["department_id"] = cls.settings.FRESH_DEFAULT_DEPT_ID
        ticket_object["group_id"] = cls.settings.FRESH_DEFAULT_GROUP_ID
        ticket_object["category"] = cls.settings.FRESH_DEFAULT_CATEGORY
        ticket_object["subject"] = cls.settings.FRESH_DEFAULT_SUBJECT

        ticket_object.update(deepcopy(cls.FRESH_TEMPLATES[template_name]))

        # An explicit subject wins over the template's subject
        if subject and subject.strip():
            ticket_object["subject"] = subject.strip()

        ticket_object["description"] = markdown_to_html(message)

        if not ticket_object["description"].strip():
            raise ValueError("Missing ticket content: description")
        
        cls.__create_new_ticket(ticket_object=ticket_object)
       

    def __create_new_ticket_note(cls, ticket_id:int, note_object:str):
        resp = post(f"https://{cls.settings.FRESH_DOMAIN}/api/v2/tickets/{ticket_id}/notes", 
                    headers=cls.FRESH_HEADER, 
                    json=note_object,
                    auth=(cls.settings.FRESH_KEY, "X"))
        if resp.status_code != 201:
            print(f"Ticket note was not created.\nResponse code {resp.status_code},\nmessage: {resp.json()}")
        else:
            response = resp.json()
            print(f"Note successfully created")


    def add_note(cls, ticket_id:int, message: str):
        note = {
            "private": True,
            "body": ""
        }

        note["body"] = markdown_to_html(message)

        if not note["body"].strip():
            raise ValueError("Missing note content: body")
        
        cls.__create_new_ticket_note(ticket_id=ticket_id, note_object=note)
