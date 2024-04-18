import csv
import io
import json
import logging
import os
import pathlib
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from datetime import datetime

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

def main():
    set_verbosity(enable_verbosity=True)

    if "INPUT_SIZE-DELTAS-REPORTS-ARTIFACT-NAME" in os.environ:
        print("::warning::The size-deltas-report-artifact-name input is deprecated. Use the equivalent input: "
              "sketches-reports-source instead.")
        os.environ["INPUT_SKETCHES-REPORTS-SOURCE"] = os.environ["INPUT_SIZE-DELTAS-REPORTS-ARTIFACT-NAME"]

    report_size_deltas = ReportSizeDeltas(repository_name=os.environ["GITHUB_REPOSITORY"],
                                          sketches_reports_source=os.environ["INPUT_SKETCHES-REPORTS-SOURCE"],
                                          token=os.environ["INPUT_GITHUB-TOKEN"],
                                          pr_number=os.environ["INPUT_PR-NUMBER"] if "INPUT_PR-NUMBER" in os.environ else None)

    report_size_deltas.report_size_deltas()


def set_verbosity(enable_verbosity):
    """Turn debug output on or off.

    Keyword arguments:
    enable_verbosity -- this will generally be controlled via the script's --verbose command line argument
                              (True, False)
    """
    # DEBUG: automatically generated output and all higher log level output
    # INFO: manually specified output and all higher log level output
    verbose_logging_level = logging.DEBUG

    if type(enable_verbosity) is not bool:
        raise TypeError
    if enable_verbosity:
        logger.setLevel(level=verbose_logging_level)
    else:
        logger.setLevel(level=logging.WARNING)

def numerical_sort(value):
    numbers = re.compile(r'(\d+)')
    parts = numbers.split(value)
    parts[1::2] = map(int, parts[1::2])
    return parts

class ReportSizeDeltas:
    """Methods for creating and submitting the memory usage change reports

    Keyword arguments:
    repository_name -- repository owner and name e.g., octocat/Hello-World
    artifact_name -- name of the workflow artifact that contains the memory usage data
    token -- GitHub access token
    pr_number -- pull request number (optional, default: None)
    """
    report_key_beginning = "Memory usage test (comparing PR agains master branch)"
    not_applicable_indicator = "N/A"

    class ReportKeys:
        """Key names used in the sketches report dictionary"""
        boards = "boards"
        board = "board"
        commit_hash = "commit_hash"
        commit_url = "commit_url"
        sizes = "sizes"
        warnings = "warnings"
        name = "name"
        absolute = "absolute"
        relative = "relative"
        current = "current"
        previous = "previous"
        delta = "delta"
        minimum = "minimum"
        maximum = "maximum"
        sketches = "sketches"
        library = "library"
        target = "target"
        compilation_success = "compilation_success"
        flash_bytes = "flash_bytes"
        flash_percentage = "flash_percentage"
        ram_bytes = "ram_bytes"
        ram_percentage = "ram_percentage"

    class CellKeys:
        """Key names used in the cell for each library/target"""
        succcess = "success"
        warning = "warning"
        error = "error"


    def __init__(self, repository_name, sketches_reports_source, token, pr_number=None):
        self.repository_name = repository_name
        self.sketches_reports_source = sketches_reports_source
        self.token = token
        self.pr_number = pr_number

    def report_size_deltas(self):
        """Comment a report of memory usage change to pull request(s)."""
        if os.environ["GITHUB_EVENT_NAME"] == "pull_request":
            # The sketches reports will be in a local folder location specified by the user
            self.report_size_deltas_from_local_reports()

        # Workaround for Pull request from forks.
        elif os.environ["GITHUB_EVENT_NAME"] == "workflow_run":
            self.report_size_deltas_from_local_reports_on_workflow_run()

        elif os.environ["GITHUB_EVENT_NAME"] == "schedule":
            self.report_size_deltas_from_local_reports_on_schedule()

        elif os.environ["GITHUB_EVENT_NAME"] == "push":
            self.report_size_deltas_from_local_reports_on_schedule()

        else:
            # The script is being run from a workflow triggered by something other than a PR
            # Scan the repository's pull requests and comment memory usage change reports where appropriate.
            self.report_size_deltas_from_workflow_artifacts()

    def report_size_deltas_from_local_reports(self):
        """Comment a report of memory usage change to the pull request."""
        sketches_reports_folder = pathlib.Path(os.environ["GITHUB_WORKSPACE"], self.sketches_reports_source)
        sketches_reports = self.get_sketches_reports(artifact_folder_object=sketches_reports_folder)

        if sketches_reports:
            report = self.generate_report(sketches_reports=sketches_reports)

            pr_number = self.pr_number
            print("::debug::PR number: " + str(pr_number))

            self.comment_report(pr_number=pr_number, report_markdown=report)

    def report_size_deltas_from_local_reports_on_workflow_run(self):
        """Comment a report of memory usage change to the pull request."""
        sketches_reports_folder = pathlib.Path(os.environ["GITHUB_WORKSPACE"], self.sketches_reports_source+"/pr")
        sketches_reports = self.get_sketches_reports(artifact_folder_object=sketches_reports_folder)

        # Read master branch sketches reports
        master_sketches_reports_folder = pathlib.Path(os.environ["GITHUB_WORKSPACE"], self.sketches_reports_source+"/master")
        master_sketches_reports = self.get_sketches_reports(artifact_folder_object=master_sketches_reports_folder)

        if sketches_reports:
            report = self.generate_report(sketches_reports=sketches_reports, master_sketches_reports=master_sketches_reports)

            # Get the PR number and remove all non-numeric characters
            pr_number = self.pr_number
            pr_number = re.sub("[^0-9]", "", pr_number)
            print("::debug::PR number: " + str(pr_number))

            self.comment_report(pr_number=pr_number, report_markdown=report)


    def report_size_deltas_from_local_reports_on_schedule(self):
        """Comment a report of memory usage change to the file ."""
        report_destination = os.environ["INPUT_DESTINATION-FILE"]
        sketches_reports_folder = pathlib.Path(os.environ["GITHUB_WORKSPACE"], self.sketches_reports_source)
        sketches_reports = self.get_sketches_reports(artifact_folder_object=sketches_reports_folder)

        if sketches_reports:
            report = self.generate_report(sketches_reports=sketches_reports)

            # datetime object containing current date and time
            now = datetime.now()
            
            # dd/mm/YY H:M:S
            dt_string = now.strftime("%b-%d-%Y %H:%M:%S")
            print("date and time =", dt_string)

            with open(report_destination, "w+") as file:
                file.write(report)
                file.write("\nGenerated on: " + dt_string + "\n")

    def report_size_deltas_from_workflow_artifacts(self):
        """Scan the repository's pull requests and comment memory usage change reports where appropriate."""
        # Get the repository's pull requests
        
        logger.debug("Getting PRs for " + self.repository_name)
        page_number = 1
        page_count = 1
        while page_number <= page_count:
            api_data = self.api_request(request="repos/" + self.repository_name + "/pulls",
                                        page_number=page_number)
            prs_data = api_data["json_data"]
            for pr_data in prs_data:
                # Note: closed PRs are not listed in the API response
                pr_number = pr_data["number"]
                pr_head_sha = pr_data["head"]["sha"]
                print("::debug::Processing pull request number:", pr_number)
                # When a PR is locked, only collaborators may comment. The automatically generated GITHUB_TOKEN will
                # likely be used, which is owned by the github-actions bot, who doesn't have collaborator status. So
                # locking the thread would cause the job to fail.
                if pr_data["locked"]:
                    print("::debug::PR locked, skipping")
                    continue

                if self.report_exists(pr_number=pr_number,
                                    pr_head_sha=pr_head_sha):
                    # Go on to the next PR
                    print("::debug::Report already exists")
                    continue

                artifact_download_url = self.get_artifact_download_url_for_sha(
                    pr_user_login=pr_data["user"]["login"],
                    pr_head_ref=pr_data["head"]["ref"],
                    pr_head_sha=pr_head_sha)
                if artifact_download_url is None:
                    # Go on to the next PR
                    print("::debug::No sketches report artifact found")
                    continue

                artifact_folder_object = self.get_artifact(artifact_download_url=artifact_download_url)

                sketches_reports = self.get_sketches_reports(artifact_folder_object=artifact_folder_object)

                if sketches_reports:
                    if sketches_reports[0][self.ReportKeys.commit_hash] != pr_head_sha:
                        # The deltas report key uses the hash from the report, but the report_exists() comparison is
                        # done using the hash provided by the API. If for some reason the two didn't match, it would
                        # result in the deltas report being done over and over again.
                        print("::warning::Report commit hash doesn't match PR's head commit hash, skipping")
                        continue

                    report = self.generate_report(sketches_reports=sketches_reports)

                    self.comment_report(pr_number=pr_number, report_markdown=report)

            page_number += 1
            page_count = api_data["page_count"]

    def report_exists(self, pr_number, pr_head_sha):
        """Return whether a report has already been commented to the pull request thread for the latest workflow run

        Keyword arguments:
        pr_number -- number of the pull request to check
        pr_head_sha -- PR's head branch hash
        """
        # Get the pull request's comments
        page_number = 1
        page_count = 1
        while page_number <= page_count:
            api_data = self.api_request(request="repos/" + self.repository_name + "/issues/" + str(pr_number)
                                                + "/comments",
                                        page_number=page_number)

            comments_data = api_data["json_data"]
            for comment_data in comments_data:
                # Check if the comment is a report for the PR's head SHA
                if comment_data["body"].startswith(self.report_key_beginning + pr_head_sha):
                    return True

            page_number += 1
            page_count = api_data["page_count"]

        # No reports found for the PR's head SHA
        return False

    def get_artifact_download_url_for_sha(self, pr_user_login, pr_head_ref, pr_head_sha):
        """Return the report artifact download URL associated with the given head commit hash

        Keyword arguments:
        pr_user_login -- user name of the PR author (used to reduce number of GitHub API requests)
        pr_head_ref -- name of the PR head branch (used to reduce number of GitHub API requests)
        pr_head_sha -- hash of the head commit in the PR branch
        """
        # Get the repository's workflow runs
        page_number = 1
        page_count = 1
        while page_number <= page_count:
            api_data = self.api_request(request="repos/" + self.repository_name + "/actions/runs",
                                        request_parameters="actor=" + pr_user_login + "&branch=" + pr_head_ref
                                                           + "&event=pull_request&status=completed",
                                        page_number=page_number)
            runs_data = api_data["json_data"]

            # Find the runs with the head SHA of the PR (there may be multiple runs)
            for run_data in runs_data["workflow_runs"]:
                if run_data["head_sha"] == pr_head_sha:
                    # Check if this run has the artifact we're looking for
                    artifact_download_url = self.get_artifact_download_url_for_run(run_id=run_data["id"])
                    if artifact_download_url is not None:
                        return artifact_download_url

            page_number += 1
            page_count = api_data["page_count"]

        # No matching artifact found
        return None

    def get_artifact_download_url_for_run(self, run_id):
        """Return the report artifact download URL associated with the given GitHub Actions workflow run

        Keyword arguments:
        run_id -- GitHub Actions workflow run ID
        """
        # Get the workflow run's artifacts
        page_number = 1
        page_count = 1
        while page_number <= page_count:
            api_data = self.api_request(request="repos/" + self.repository_name + "/actions/runs/"
                                                + str(run_id) + "/artifacts",
                                        page_number=page_number)
            artifacts_data = api_data["json_data"]

            for artifact_data in artifacts_data["artifacts"]:
                # The artifact is identified by a specific name
                if not artifact_data["expired"] and artifact_data["name"] == self.sketches_reports_source:
                    return artifact_data["archive_download_url"]

            page_number += 1
            page_count = api_data["page_count"]

        # No matching artifact found
        return None

    def get_artifact(self, artifact_download_url):
        """Download and unzip the artifact and return an object for the temporary directory containing it

        Keyword arguments:
        artifact_download_url -- URL to download the artifact from GitHub
        """
        # Create temporary folder
        artifact_folder_object = tempfile.TemporaryDirectory(prefix="reportsizedeltas-")
        try:
            # Download artifact
            with open(file=artifact_folder_object.name + "/" + self.sketches_reports_source + ".zip",
                      mode="wb") as out_file:
                with self.raw_http_request(url=artifact_download_url) as fp:
                    out_file.write(fp.read())

            # Unzip artifact
            artifact_zip_file = artifact_folder_object.name + "/" + self.sketches_reports_source + ".zip"
            with zipfile.ZipFile(file=artifact_zip_file, mode="r") as zip_ref:
                zip_ref.extractall(path=artifact_folder_object.name)
            os.remove(artifact_zip_file)

            return artifact_folder_object

        except Exception:
            artifact_folder_object.cleanup()
            raise

    def get_sketches_reports(self, artifact_folder_object):
        """Parse the artifact files and return a list containing the data.

        Keyword arguments:
        artifact_folder_object -- object containing the data about the temporary folder that stores the markdown files
        """
        with artifact_folder_object as artifact_folder:
            # artifact_folder will be a string when running in non-local report mode
            artifact_folder = pathlib.Path(artifact_folder)
            sketches_reports = []
            print("::debug::Artifact folder: " + str(artifact_folder))
            for report_filename in sorted(artifact_folder.iterdir(), key=lambda x: numerical_sort(x.name)):
                print("::debug::Report filename: " + str(report_filename))
                # Combine sketches reports into an array
                with open(file=report_filename.joinpath(report_filename)) as report_file:
                    report_data = json.load(report_file)
                    #print("::debug::Report data: " + str(report_data))
                    for fqbn_data in report_data[self.ReportKeys.boards]:
                        #if self.ReportKeys.sizes in fqbn_data:
                        # The report contains deltas data
                        #print("::debug::Sketches report data: " + str(fqbn_data))
                        sketches_reports.append(fqbn_data)

        if not sketches_reports:
            print("No size deltas data found in workflow artifact for this PR. The compile-examples action's "
                  "enable-size-deltas-report input must be set to true to produce size deltas data.")
            return None
            
        consolidated_boards = {}

        for item in sketches_reports:
            board = item[self.ReportKeys.board]
            target = item[self.ReportKeys.target]
            sketches = item[self.ReportKeys.sketches]
            
            if board not in consolidated_boards:
                consolidated_boards[board] = {self.ReportKeys.target: target, self.ReportKeys.sketches: sketches}
            else:
                consolidated_boards[board][self.ReportKeys.sketches].extend(sketches)

        consolidated_list = [{self.ReportKeys.board: board, self.ReportKeys.target: data[self.ReportKeys.target], self.ReportKeys.sketches: data[self.ReportKeys.sketches]} for board, data in consolidated_boards.items()]
        #print("::debug::Consolidated sketches reports: " + str(consolidated_list))

        #return sketches_reports
        return consolidated_list

    def generate_report(self, sketches_reports, master_sketches_reports=None):
        """Return the Markdown for the deltas report comment.

        Keyword arguments:
        sketches_reports -- list of sketches_reports containing the data to generate the deltas report from
        """
        # From https://github.community/t/maximum-length-for-the-comment-body-in-issues-and-pr/148867/2
        # > PR body/Issue comments are still stored in MySQL as a mediumblob with a maximum value length of 262,144.
        # > This equals a limit of 65,536 4-byte unicode characters.
        maximum_report_length = 262144

        # The report will be generated as a table
        # The table will have the following columns:

        # Targets  |   Target1   |   Target2   |   Target3   | ... so in final its |  Targets   | Target1 | Target1 | Target2 | Target2 | Target3 | Target3 | ...
        # Sketch   | FLASH | RAM | FLASH | RAM | FLASH | RAM | ...
        # Example1 |   1   |  2  |   3   |  4  |   5   |  6  | ...

        print("::debug::Generating deltas report")
        #print("::debug::Sketches reports: " + str(sketches_reports))
        #print("::debug::Master sketches reports: " + str(master_sketches_reports))

        first_row_heading = "Target"
        second_row_heading = "Example"

        # Create the summary report data with the first row of headings
        summary_report_data = [["Memory",  "FLASH [bytes]", "FLASH [%]", "RAM [bytes]", "RAM [%]"],["Target", "Min", "Max", "Min", "Max", "Min", "Max", "Min", "Max"]]

        # Create detailed report data
        detailed_report_data = [[first_row_heading],[second_row_heading]]

        row_number = 0
        column_number = 1

        board_count = 0
        for fqbns_data in sketches_reports:
            #for boards in fqbns_data[self.ReportKeys.boards]:
            board_count += 1

        print("::debug::Board count: " + str(board_count))

        # Add the FLASH and RAM to the second row
        for i in range(board_count):
            detailed_report_data[1].extend(["FLASH","RAM"])

        # Debug print detailed report data
        logger.debug("Detailed report data:\n" + str(detailed_report_data))

        for board in sketches_reports:
            #for boards in fqbns_data[self.ReportKeys.boards]:
            detailed_report_data[0].append(board[self.ReportKeys.target].upper())

            flash_b_max = 0
            flash_b_min = 0
            flash_p_max = 0
            flash_p_min = 0
            ram_b_max = 0
            ram_b_min = 0
            ram_p_max = 0
            ram_p_min = 0

            # Populate the row with data
            for sketch in board[self.ReportKeys.sketches]:
                cell_value = {}
                sketch_name = sketch[self.ReportKeys.name]
                # Remove the "libraries/" prefix from the sketch name
                sketch_name = sketch_name.replace("libraries/", "")
                
                # Determine row number for sketch
                position = get_report_row_number(
                    report=detailed_report_data,
                    row_heading=sketch_name
                )
                if position == 0:
                    # Add a row to the report
                    #row = [ "N/A" for i in boards]
                    row = [sketch_name]
                    for i in range(board_count):
                        row.extend(["",""])
                    #row.extend(dict(zip(cell_key_list, [0]*len(cell_key_list))) for x in range(board_count))
                    #row.append("N/A")
                    #row[0] = library_name
                    detailed_report_data.append(row)
                    row_number = len(detailed_report_data) - 1
                    #cell_value = dict(zip(cell_key_list, [0]*len(cell_key_list)))
                else:
                    row_number = position
                    #cell_value = summary_report_data[row_number][column_number]

                # Add data to the report for FLASH and RAM, can be changed to absolute or relative
                print("::debug::Sketch: " + str(sketch))

                #Find the master sketch data for the same sketch and board and save it as a dictionary
                master_sketch = None
                if master_sketches_reports:
                    for master_board in master_sketches_reports:
                        if master_board[self.ReportKeys.target] == board[self.ReportKeys.target]:
                            for loop_master_sketch in master_board[self.ReportKeys.sketches]:
                                if loop_master_sketch[self.ReportKeys.name] == sketch[self.ReportKeys.name]:
                                    master_sketch = loop_master_sketch
                                    break

                print("::debug::Master sketch: " + str(master_sketch))
                #Calculate the deltas, master sketch is the main sketch for comparison
                #each skech contains "sizes" key with valus "flash_bytes", "flash_percentage", "ram_bytes", "ram_percentage"
                if master_sketch != None:
                    flash_delta_bytes = sketch[self.ReportKeys.sizes][0][self.ReportKeys.flash_bytes] - master_sketch[self.ReportKeys.sizes][0][self.ReportKeys.flash_bytes]
                    #flash_delta_percentage = sketch[self.ReportKeys.sizes][0][self.ReportKeys.flash_percentage] - master_sketch[self.ReportKeys.sizes][0][self.ReportKeys.flash_percentage]
                    ram_delta_bytes = sketch[self.ReportKeys.sizes][0][self.ReportKeys.ram_bytes] - master_sketch[self.ReportKeys.sizes][0][self.ReportKeys.ram_bytes]
                    #ram_delta_percentage = sketch[self.ReportKeys.sizes][0][self.ReportKeys.ram_percentage] - master_sketch[self.ReportKeys.sizes][0][self.ReportKeys.ram_percentage]

                    #Calculate percentage change from flash_bytes and ram_bytes
                    flash_delta_percentage = float(((sketch[self.ReportKeys.sizes][0][self.ReportKeys.flash_bytes] / 
                                              master_sketch[self.ReportKeys.sizes][0][self.ReportKeys.flash_bytes]) * 100) - 100)
                    
                    ram_delta_percentage = float(((sketch[self.ReportKeys.sizes][0][self.ReportKeys.ram_bytes] /
                                                master_sketch[self.ReportKeys.sizes][0][self.ReportKeys.ram_bytes]) * 100) - 100)
                    
                    if flash_delta_bytes > flash_b_max:
                        flash_b_max = flash_delta_bytes
                    if flash_delta_bytes < flash_b_min:
                        flash_b_min = flash_delta_bytes
                    if flash_delta_percentage > flash_p_max:
                        flash_p_max = flash_delta_percentage
                    if flash_delta_percentage < flash_p_min:
                        flash_p_min = flash_delta_percentage
                    if ram_delta_bytes > ram_b_max:
                        ram_b_max = ram_delta_bytes
                    if ram_delta_bytes < ram_b_min:
                        ram_b_min = ram_delta_bytes
                    if ram_delta_percentage > ram_p_max:
                        ram_p_max = ram_delta_percentage
                    if ram_delta_percentage < ram_p_min:
                        ram_p_min = ram_delta_percentage

                    detailed_report_data[row_number][column_number] = flash_delta_bytes
                    detailed_report_data[row_number][column_number+1] = ram_delta_bytes
                else:
                    detailed_report_data[row_number][column_number] = ""
                    detailed_report_data[row_number][column_number+1] = ""

                #detailed_report_data[row_number][column_number] = sketch[self.ReportKeys.sizes][0][self.ReportKeys.delta][self.ReportKeys.absolute]
                #detailed_report_data[row_number][column_number+1] = sketch[self.ReportKeys.sizes][1][self.ReportKeys.delta][self.ReportKeys.absolute]

            # Add data to the summary report
            summary_report_data.append([board[self.ReportKeys.target].upper(),
                                        flash_b_min, flash_b_max,
                                        flash_p_min, flash_p_max,
                                        ram_b_min, ram_b_max,
                                        ram_p_min, ram_p_max])
            
                                        #get_summary_value(self, True, flash_b_min, flash_b_max),
                                        #get_summary_value(self, True, flash_p_min, flash_p_max),
                                        #get_summary_value(self, True, ram_b_min, ram_b_max),
                                        #get_summary_value(self, True, ram_p_min, ram_p_max)])
            column_number += 2

        logger.debug("Summary report data:\n" + str(summary_report_data))

        emoji_decreased = ":green_heart:"
        emoji_increased = ":bangbang:"
        emoji_warning = ":warning:"

        # Process detailed report data with emojis
        for row in range(2,len(detailed_report_data)):
            for cell in range(1,len(detailed_report_data[row])):
                print_result = ""

                if str(detailed_report_data[row][cell]) != "":
                    if int(detailed_report_data[row][cell]) > 0 and int(detailed_report_data[row][cell]) < 2048:
                        print_result = emoji_warning + "+"
                    if int(detailed_report_data[row][cell]) > 2047:
                        print_result = emoji_increased + "+"
                    if int(detailed_report_data[row][cell]) < 0:
                        print_result = emoji_decreased
                    #if detailed_report_data[row][cell] is bigger than 2000 or less than -2000, do /1024 and add "KB"
                    if int(detailed_report_data[row][cell]) > 2048 or int(detailed_report_data[row][cell]) < -2048:
                        print_result += str(round(int(detailed_report_data[row][cell])/1024)) + "K"
                    else:
                        print_result += str(detailed_report_data[row][cell])
                    
                else:
                    print_result = "-"

                detailed_report_data[row][cell] = print_result

        # Process summary report data with emojis
        for row in range(2,len(summary_report_data)):
            for cell in range(1,len(summary_report_data[row])):
                print_result = ""

                #cells 1,2,5,6 are integers, 3,4,7,8 are floats take care of them
                if str(summary_report_data[row][cell]) != "":
                    if cell == 1 or cell == 2 or cell == 5 or cell == 6:
                        if int(summary_report_data[row][cell]) > 0 and int(summary_report_data[row][cell]) < 2048:
                            print_result = emoji_warning + "+"
                        if int(summary_report_data[row][cell]) > 2047:
                            print_result = emoji_increased + "+"
                        if int(summary_report_data[row][cell]) < 0:
                            print_result = emoji_decreased
                        #if summary_report_data[row][cell] is bigger than 2000 or less than -2000, do /1024 and add "KB"
                        if int(summary_report_data[row][cell]) > 2047 or int(summary_report_data[row][cell]) < -2048:
                            print_result += str(round(int(summary_report_data[row][cell])/1024)) + "K"
                        else:
                            print_result += str(summary_report_data[row][cell])
                    else:
                        if float(summary_report_data[row][cell]) > 0:
                            print_result = emoji_warning + "+"
                        if float(summary_report_data[row][cell]) > 1:
                            print_result = emoji_increased + "+"
                        if float(summary_report_data[row][cell]) < 0:
                            print_result = emoji_decreased
                        #add float value to the print result with 2 decimal points
                        print_result += "{:.2f}".format(summary_report_data[row][cell])
                else:
                    print_result = "-"

                summary_report_data[row][cell] = print_result
        
        # Add comment heading
        report_markdown = "### " + self.report_key_beginning + "\n\n"

        # Add info on what is in table 
        report_markdown = report_markdown + "The table below shows the summary of memory usage change (min - max) in bytes and percentage for each target.\n\n"

        # Add summary table
        report_markdown = report_markdown + generate_summary_html_table(row_list=summary_report_data) + "\n"

        # Add markdown detail to hide the table
        report_markdown = report_markdown + "<details>\n<summary>Click to expand the detailed deltas report [usage change in BYTES]</summary>\n\n"

        # Add detailed table
        report_markdown = report_markdown + generate_detailed_html_table(row_list=detailed_report_data) + "\n"

        # Add markdown detail to hide the table
        report_markdown = report_markdown + "</details>\n"
        print("Report:\n" + report_markdown)
        return report_markdown

    def comment_report(self, pr_number, report_markdown):
        """Submit the report as a comment on the PR thread

        Keyword arguments:
        pr_number -- pull request number to submit the report to
        report_markdown -- Markdown formatted report
        """
        print("::debug::Adding deltas report comment to pull request")
        report_data = {"body": report_markdown}
        report_data = json.dumps(obj=report_data)
        report_data = report_data.encode(encoding="utf-8")
        url = ("https://api.github.com/repos/"
               + self.repository_name
               + "/issues/"
               + str(pr_number)
               + "/comments")

        self.http_request(url=url, data=report_data)

    def api_request(self, request, request_parameters="", page_number=1):
        """Do a GitHub API request. Return a dictionary containing:
        json_data -- JSON object containing the response
        additional_pages -- indicates whether more pages of results remain (True, False)
        page_count -- total number of pages of results

        Keyword arguments:
        request -- the section of the URL following https://api.github.com/
        request_parameters -- GitHub API request parameters (see: https://developer.github.com/v3/#parameters)
                              (default value: "")
        page_number -- Some responses will be paginated. This argument specifies which page should be returned.
                       (default value: 1)
        """
        return self.get_json_response(url="https://api.github.com/" + request + "?" + request_parameters + "&page="
                                          + str(page_number) + "&per_page=100")

    def get_json_response(self, url):
        """Load the specified URL and return a dictionary:
        json_data -- JSON object containing the response
        additional_pages -- indicates whether more pages of results remain (True, False)
        page_count -- total number of pages of results

        Keyword arguments:
        url -- the URL to load
        """
        try:
            response_data = self.http_request(url=url)
            try:
                json_data = json.loads(response_data["body"])
            except json.decoder.JSONDecodeError as exception:
                # Output some information on the exception
                logger.warning(str(exception.__class__.__name__) + ": " + str(exception))
                # pass on the exception to the caller
                raise exception

            if not json_data:
                # There was no HTTP error but an empty list was returned (e.g. pulls API request when the repo
                # has no open PRs)
                page_count = 0
                additional_pages = False
            else:
                page_count = get_page_count(link_header=response_data["headers"]["Link"])
                if page_count > 1:
                    additional_pages = True
                else:
                    additional_pages = False

            return {"json_data": json_data, "additional_pages": additional_pages, "page_count": page_count}
        except Exception as exception:
            raise exception

    def http_request(self, url, data=None):
        """Make a request and return a dictionary:
        read -- the response
        info -- headers
        url -- the URL of the resource retrieved

        Keyword arguments:
        url -- the URL to load
        data -- data to pass with the request
                (default value: None)
        """
        with self.raw_http_request(url=url, data=data) as response_object:
            return {"body": response_object.read().decode(encoding="utf-8", errors="ignore"),
                    "headers": response_object.info(),
                    "url": response_object.geturl()}

    def raw_http_request(self, url, data=None):
        """Make a request and return an object containing the response.

        Keyword arguments:
        url -- the URL to load
        data -- data to pass with the request
                (default value: None)
        """
        # Maximum times to retry opening the URL before giving up
        maximum_urlopen_retries = 3

        logger.info("Opening URL: " + url)

        # GitHub recommends using user name as User-Agent (https://developer.github.com/v3/#user-agent-required)
        headers = {"Authorization": "token " + self.token, "User-Agent": self.repository_name.split("/")[0]}
        request = urllib.request.Request(url=url, headers=headers, data=data)

        retry_count = 0
        while retry_count <= maximum_urlopen_retries:
            retry_count += 1
            try:
                # The rate limit API is not subject to rate limiting
                if url.startswith("https://api.github.com") and not url.startswith("https://api.github.com/rate_limit"):
                    self.handle_rate_limiting()
                return urllib.request.urlopen(url=request)
            except Exception as exception:
                if not determine_urlopen_retry(exception=exception):
                    raise exception

        # Maximum retries reached without successfully opening URL
        raise TimeoutError("Maximum number of URL load retries exceeded")

    def handle_rate_limiting(self):
        """Check whether the GitHub API request limit has been reached.
        If so, exit with exit status 0.
        """
        rate_limiting_data = self.get_json_response(url="https://api.github.com/rate_limit")["json_data"]
        # GitHub has two API types, each with their own request limits and counters.
        # "search" applies only to api.github.com/search.
        # "core" applies to all other parts of the API.
        # Since this code only uses the "core" API, only those values are relevant
        logger.debug("GitHub core API request allotment: " + str(rate_limiting_data["resources"]["core"]["limit"]))
        logger.debug("Remaining API requests: " + str(rate_limiting_data["resources"]["core"]["remaining"]))
        logger.debug("API request count reset time: " + str(rate_limiting_data["resources"]["core"]["reset"]))

        if rate_limiting_data["resources"]["core"]["remaining"] == 0:
            # GitHub uses a fixed rate limit window of 60 minutes. The window starts when the API request count goes
            # from 0 to 1. 60 minutes after the start of the window, the request count is reset to 0.
            print("::warning::GitHub API request quota has been reached. Giving up for now.")
            sys.exit(0)


def determine_urlopen_retry(exception):
    """Determine whether the exception warrants another attempt at opening the URL.
    If so, delay then return True. Otherwise, return False.

    Keyword arguments:
    exception -- the exception
    """
    # Retry urlopen after exceptions that start with the following strings
    urlopen_retry_exceptions = [
        # urllib.error.HTTPError: HTTP Error 403: Forbidden
        "HTTPError: HTTP Error 403",
        # urllib.error.HTTPError: HTTP Error 502: Bad Gateway
        "HTTPError: HTTP Error 502",
        # urllib.error.HTTPError: HTTP Error 503: Service Unavailable
        # caused by rate limiting
        "HTTPError: HTTP Error 503",
        # http.client.RemoteDisconnected: Remote end closed connection without response
        "RemoteDisconnected",
        # ConnectionResetError: [Errno 104] Connection reset by peer
        "ConnectionResetError",
        # ConnectionRefusedError: [WinError 10061] No connection could be made because the target machine actively
        # refused it
        "ConnectionRefusedError",
        # urllib.error.URLError: <urlopen error [WinError 10061] No connection could be made because the target
        # machine actively refused it>
        "<urlopen error [WinError 10061] No connection could be made because the target machine actively refused "
        "it>"
    ]

    # Delay before retry (seconds)
    urlopen_retry_delay = 30

    exception_string = str(exception.__class__.__name__) + ": " + str(exception)
    logger.info(exception_string)
    for urlopen_retry_exception in urlopen_retry_exceptions:
        if str(exception_string).startswith(urlopen_retry_exception):
            # These errors may only be temporary, retry
            logger.warning("Temporarily unable to open URL (" + str(exception) + "), retrying")
            time.sleep(urlopen_retry_delay)
            return True

    # Other errors are probably permanent so give up
    if str(exception_string).startswith("HTTPError: HTTP Error 401"):
        # Give a nice hint as to the cause of this error
        print("::error::HTTP Error 401 may be caused by providing an incorrect GitHub personal access token.")
    return False


def get_page_count(link_header):
    """Return the number of pages of the API response

    Keyword arguments:
    link_header -- Link header of the HTTP response
    """
    page_count = 1
    if link_header is not None:
        # Get the pagination data
        for link in link_header.split(","):
            if link[-13:] == ">; rel=\"last\"":
                link = re.split("[?&>]", link)
                for parameter in link:
                    if parameter[:5] == "page=":
                        page_count = int(parameter.split("=")[1])
                        break
                break
    return page_count

def get_report_row_number(report, row_heading):
    """Return the row number of the given heading.

    Keyword arguments:
    row_heading -- the text of the row heading. If it doesn't exist a 0 will be returned
    """
    for i in report:
        if i[0] == row_heading:
            return report.index(i)
    return 0

def generate_markdown_table(row_list):
    """Return the data formatted as a Markdown table

    Keyword arguments:
    row_list -- list containing the data
    """
    # Generate heading row
    markdown_table = "|".join([str(cell) for cell in row_list[0]]) + "\n"
    # Add divider row
    markdown_table = markdown_table + "-|" + "|".join([":-:" for _ in row_list[0][:-1]]) + "\n"
    # Add data rows
    for row in row_list[1:]:
        markdown_table = markdown_table + "|".join([str(cell) for cell in row]) + "\n"

    return markdown_table

def generate_detailed_html_table(row_list):
    """Return the data formatted as a Markdown table

    Keyword arguments:
    row_list -- list containing the data
    """

    html_table = "<table>\n"
    # Generate heading row
    #html_table = html_table + "<tr>" + "".join(["<th>" + str(cell) + "</th>" for cell in row_list[0]]) + "</tr>\n"
    html_table = html_table + "<tr>" + "".join(["<th colspan='2' align='center' nowrap>" + str(cell) + "</th>" if index != 0 else "<th nowrap>" + str(cell) + "</th>" for index, cell in enumerate(row_list[0])]) + "</tr>\n"

    # Add data rows
    for row in row_list[1:]:
        html_table = html_table + "<tr>" + "".join(["<td nowrap>" + str(cell) + "</td>" for cell in row]) + "</tr>\n"

    html_table = html_table + "</table>\n"

    return html_table

def generate_summary_html_table(row_list):
    """Return the data formatted as a Markdown table

    Keyword arguments:
    row_list -- list containing the data
    """

    #example of the table
    #<tr>
    #    <th>Memory</th><th colspan='2' align='center'>FLASH [bytes]</th>
    #    <th colspan='2' align='center'>FLASH [%]</th>
    #    <th colspan='2' align='center'>RAM [bytes]</th>
    #    <th colspan='2' align='center'>RAM [%]</th></tr>
    #<tr>
    #    <th>Target</th>
    #    <th align='center'>min</th>
    #    <th align='center'>max</th>
    #    <th align='center'>min</th>
    #    <th align='center'>max</th>
    #    <th align='center'>min</th>
    #    <th align='center'>max</th>
    #    <th align='center'>min</th>
    #    <th align='center'>max</th>
    #</tr>
    #<tr>
    #    <td>ESP32S3</td>
    #    <td align='center'>:green_heart:-15876</td>
    #    <td align='right'>:small_red_triangle:+216</td>
    #    <td align='center'>:green_heart:-1</td>
    #    <td align='center'>0</td>
    #    <td align='center'>:green_heart:-472</td>
    #    <td align='center'>:small_red_triangle:+48</td>
    #    <td align='center'>:green_heart:-1</td>
    #    <td align='center'>0</td>
    #</tr>

    html_table = "<table>\n"
    # Generate heading row
    #html_table = html_table + "<tr>" + "".join(["<th>" + str(cell) + "</th>" for cell in row_list[0]]) + "</tr>\n"
    html_table = html_table + "<tr>" + "".join(["<th colspan='2' align='center' nowrap>" + str(cell) + "</th>" if index != 0 else "<th nowrap>" + str(cell) + "</th>" for index, cell in enumerate(row_list[0])]) + "</tr>\n"

    # Add data rows
    for row in row_list[1:]:
        html_table = html_table + "<tr>" + "".join(["<td align='center' nowrap>" + str(cell) + "</td>" if index != 0 else "<td nowrap>" + str(cell) + "</td>" for index, cell in enumerate(row)]) + "</tr>\n"

    html_table = html_table + "</table>\n"

    return html_table

def generate_csv_table(row_list):
    """Return a string containing the supplied data formatted as CSV.

    Keyword arguments:
    row_list -- list containing the data
    """
    csv_string = io.StringIO()
    csv_writer = csv.writer(csv_string, lineterminator="\n")
    for row in row_list:
        csv_writer.writerow(row)

    return csv_string.getvalue()

def splitall(path):
    allparts = []
    while 1:
        parts = os.path.split(path)
        if parts[0] == path:  # sentinel for absolute paths
            allparts.insert(0, parts[0])
            break
        elif parts[1] == path: # sentinel for relative paths
            allparts.insert(0, parts[1])
            break
        else:
            path = parts[0]
            allparts.insert(0, parts[1])
    return allparts

def get_summary_value(self, show_emoji: bool, minimum, maximum) -> str:
    """Return the Markdown formatted text for a memory change data cell in the report table.

    Keyword arguments:
    show_emoji -- whether to add the emoji change indicator
    minimum -- minimum amount of change for this memory type
    maximum -- maximum amount of change for this memory type
    """
    size_decrease_emoji = ":green_heart:"
    size_ambiguous_emoji = ":grey_question:"
    size_increase_emoji = ":small_red_triangle:"

    value = None
    if minimum == self.not_applicable_indicator:
        value = self.not_applicable_indicator
        emoji = None
    elif minimum < 0 and maximum <= 0:
        emoji = size_decrease_emoji
    elif minimum == 0 and maximum == 0:
        emoji = None
    elif minimum >= 0 and maximum > 0:
        emoji = size_increase_emoji
    else:
        emoji = size_ambiguous_emoji

    if value is None:
        # Prepend + to positive values to improve readability
        if minimum > 0:
            minimum = "+" + str(minimum)
        if maximum > 0:
            maximum = "+" + str(maximum)

        value = str(minimum) + " - " + str(maximum)

    if show_emoji and (emoji is not None):
        value = emoji + " " + value

    return value

# Only execute the following code if the script is run directly, not imported
if __name__ == "__main__":
    main()  # pragma: no cover
