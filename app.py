import base64
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, jsonify
import mysql.connector
import boto3
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from botocore.exceptions import NoCredentialsError
import os
import io
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import matplotlib.pyplot as plt

app = Flask(__name__)
app.secret_key = "Your-secret-key-here"
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB

# Database connection credentials
db_config = {
    "host": "Your-host-name",
    "user": "Your-user-name",
    "password": "Your-password-here",
    "database": "Your-Database-here"
}

# S3 configuration
s3_client = boto3.client('s3',
                         aws_access_key_id='Your-AWS-Access-key',
                         aws_secret_access_key='Your-Secret-Key',
                         region_name='Your-Region-name')

BUCKET_NAME = 'Your-Bucket-name'

# Create a ThreadPoolExecutor
executor = ThreadPoolExecutor(max_workers=5)

# Function to check login credentials (username and password)
def check_login(username, password):
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor()
    
    query = "SELECT password FROM user_credentials WHERE username = %s"
    cursor.execute(query, (username,))
    result = cursor.fetchone()
    
    cursor.close()
    conn.close()
    
    if result:
        stored_password_hash = result[0]
        if stored_password_hash == password:
            return True
    return False

# Route for the login page
@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        if check_login(username, password):
            session['logged_in'] = True
            flash("Login successful!", "success")
            return redirect(url_for('dashboard'))
        else:
            flash("Invalid username or password", "error")
            return redirect(url_for('login'))

    return render_template('login.html')

# Route for the dashboard after successful login
@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'):
        flash("Please log in to access this page.", "error")
        return redirect(url_for('login'))
    return render_template('dashboard.html')

# Function to upload file to S3
def upload_to_s3(file, bucket_name, object_name):
    try:
        s3_client.upload_fileobj(file, bucket_name, object_name)
        return True
    except Exception as e:
        print(f"Error uploading to S3: {str(e)}")
        return False

# Function to insert version details into the database
def insert_version_details(merged_filename, application_name, version_code, version_description):
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        
        query = """
        INSERT INTO version_control (file_name, upload_date,application_name, version_code, version_description)
        VALUES (%s, NOW(), %s, %s, %s)
        """
        cursor.execute(query, (merged_filename, application_name, version_code, version_description))
        conn.commit()
        
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error inserting into database: {str(e)}")
        return False

# Route for uploading a new version
@app.route('/upload_version', methods=['GET', 'POST'])
def upload_version():
    if not session.get('logged_in'):
        flash("Please log in to access this page.", "error")
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        application_name = request.form['application_name']
        version_code = request.form['version_code']
        version_description = request.form['version_description']
        file = request.files['file']
        
        if file:
            merged_filename = secure_filename(f"{application_name}_{version_code}.apk")
            
            # Use ThreadPoolExecutor to perform S3 upload and database insertion concurrently
            s3_upload_future = executor.submit(upload_to_s3, file, BUCKET_NAME, merged_filename)
            db_insert_future = executor.submit(insert_version_details, merged_filename, application_name, version_code, version_description)
            
            s3_upload_success = s3_upload_future.result()
            db_insert_success = db_insert_future.result()
            
            if s3_upload_success and db_insert_success:
                return jsonify({'status': 'success', 'message': 'Version uploaded successfully!'})
            else:
                return jsonify({'status': 'error', 'message': 'An error occurred during upload.'})
    
    return render_template('upload_version.html')

@app.route('/application_list')
def application_list():
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        query = "SELECT DISTINCT application_name FROM version_control"
        cursor.execute(query)
        applications = cursor.fetchall()
        cursor.close()
        conn.close()
        
        return render_template('application_list.html', applications=applications)

    except mysql.connector.Error as err:
        print("Database error:", err)
        flash("An error occurred while fetching data.", "error")
        return redirect(url_for('version_management'))
        
@app.route('/version_management/<application_name>')
def version_management(application_name):
    filter_option = request.args.get('filter', 'all')
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        # Fetch versions for the selected application
        query = """
        SELECT version_id, version_code, file_name, upload_date, version_description 
        FROM version_control 
        WHERE application_name = %s
        """
        
        if filter_option == "current_day":
            query += " AND DATE(upload_date) = CURDATE()"
        elif filter_option == "previous_5_days":
            query += " AND upload_date >= DATE_SUB(CURDATE(), INTERVAL 5 DAY)"
        elif filter_option == "previous_10_days":
            query += " AND upload_date >= DATE_SUB(CURDATE(), INTERVAL 10 DAY)"
        elif filter_option == "previous_month":
            query += " AND upload_date >= DATE_SUB(CURDATE(), INTERVAL 1 MONTH)"
            
        cursor.execute(query, (application_name,))
        versions = cursor.fetchall()

        cursor.close()
        conn.close()
        return render_template('version_management.html', versions=versions, application_name=application_name)
    
    except mysql.connector.Error as err:
        print("Database error:", err)
        flash("An error occurred while fetching version data.", "error")
        return redirect(url_for('application_list'))


@app.route('/download/<int:version_id>')
def download(version_id):
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        query = "SELECT file_name FROM version_control WHERE version_id = %s"
        cursor.execute(query, (version_id,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result:
            file_name = result[0]
            file_key = f"{file_name}"

            try:
                file_stream = io.BytesIO()
                s3_client.download_fileobj(BUCKET_NAME, file_key, file_stream)
                file_stream.seek(0)

                return send_file(file_stream, as_attachment=True, download_name=file_name)
            except NoCredentialsError:
                flash("Credentials not available for S3 download", "error")
            except Exception as e:
                flash(f"An error occurred: {str(e)}", "error")
        else:
            flash("File not found.", "error")
    except mysql.connector.Error as err:
        flash(f"Database error: {str(err)}", "error")
    
    return redirect(url_for('dashboard'))

@app.route('/release', methods=['POST'])
def release():
    data = request.json
    version_id = data['version_id']
    drive_link = data['drive_link']
    download_count =0

    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        
        query = """
        INSERT INTO version_release(version_id, drive_link,download_count)
        VALUES (%s, %s, %s)
        """
        cursor.execute(query, (version_id, drive_link, download_count))
        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({'message': 'Drive link submitted successfully!'})
    
    except mysql.connector.Error as err:
        return jsonify({'error': f"Database error: {str(err)}"}), 500



@app.route('/links')
def links():
    if not session.get('logged_in'):
        flash("Please log in to access this page.", "error")
        return redirect(url_for('login'))

    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        query = """
        SELECT vr.release_id, vr.version_id, vr.drive_link, vr.release_date, vr.download_count,
               vc.application_name, vc.version_code
        FROM version_release vr
        JOIN version_control vc ON vr.version_id = vc.version_id
        ORDER BY vr.release_date DESC
        """
        cursor.execute(query)
        links = cursor.fetchall()

        cursor.close()
        conn.close()

        if not links:
            links = []

    except mysql.connector.Error as err:
        print("Database error:", err)
        flash("An error occurred while fetching link data.", "error")
        links = []

    return render_template('links.html', links=links)

@app.route('/download_file/<int:release_id>')
def download_file(release_id):
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)
        print(f"Requested release_id: {release_id}")  # Debug print

        # Increment the download count
        update_query = "UPDATE version_release SET download_count = download_count + 1 WHERE release_id = %s"
        cursor.execute(update_query, (release_id,))

        # Fetch the drive link
        select_query = "SELECT drive_link FROM version_release WHERE release_id = %s"
        cursor.execute(select_query, (release_id,))
        result = cursor.fetchone()

        conn.commit()
        cursor.close()
        conn.close()

        if result and result['drive_link']:
            # Redirect to the Google Drive link after incrementing the download count
            print(f"Redirecting to: {result['drive_link']}")  # Debug print
            return redirect(result['drive_link'])
        else:
            flash("Download link not found.", "error")
            return redirect(url_for('links'))

    except mysql.connector.Error as err:
        print(f"Database error: {str(err)}")  # Debug print
        flash(f"Database error: {str(err)}", "error")
        return redirect(url_for('links'))

@app.route('/download_page/<int:release_id>')
def download_page(release_id):
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        query = """
        SELECT vr.release_id, vr.drive_link, vc.application_name, vc.version_code
        FROM version_release vr
        JOIN version_control vc ON vr.version_id = vc.version_id
        WHERE vr.release_id = %s
        """
        cursor.execute(query, (release_id,))
        result = cursor.fetchone()

        cursor.close()
        conn.close()
        print(release_id)
        if result:
            return render_template('download_page.html', 
                                   release_id=release_id,
                                   application_name=result['application_name'],
                                   version_code=result['version_code'])
        else:
            flash("Download page not found.", "error")
            return redirect(url_for('links'))

    except mysql.connector.Error as err:
        print(f"Database error: {str(err)}")  # Add this line for debugging
        flash(f"Database error: {str(err)}", "error")
        return redirect(url_for('links'))
    

    

@app.route('/analytics')
def analytics():
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)
        
        # Query to get unique application names
        query = "SELECT DISTINCT application_name FROM version_control"
        cursor.execute(query)
        applications = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        return render_template('analytics.html', applications=applications)
    
    except mysql.connector.Error as err:
        print("Database error:", err)
        flash("An error occurred while fetching data.", "error")
        return redirect(url_for('links'))

@app.route('/application_graph/<application_name>')
def application_graph(application_name):
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)
        
        # Query to get version_code and download_count for a specific application
        query = """
        SELECT CONCAT(vc.application_name, ' - ', vc.version_code) AS version_label, SUM(vr.download_count) AS download_count
        FROM version_control vc
        JOIN version_release vr ON vc.version_id = vr.version_id
        WHERE vc.application_name = %s
        GROUP BY vc.application_name, vc.version_code
        """
        cursor.execute(query, (application_name,))
        download_data = cursor.fetchall()
        
        cursor.close()
        conn.close()

        # Generate the graph
        version_graph = generate_bar_graph(download_data, 'version_label', 'download_count')

        return render_template('application_graph.html', graph=version_graph, application_name=application_name)
    
    except mysql.connector.Error as err:
        print("Database error:", err)
        flash("An error occurred while fetching data.", "error")
        return redirect(url_for('analytics'))

def generate_bar_graph(data, x_label, y_label):
    try:
        # Prepare data for plotting
        x = [item[x_label] for item in data]
        y = [item[y_label] for item in data]
        
        # Create the plot
        plt.figure(figsize=(12, 8))
        plt.bar(x, y, color='skyblue')
        plt.xlabel(x_label.replace('_', ' ').title())
        plt.ylabel(y_label.replace('_', ' ').title())
        plt.title(f'{x_label.replace("_", " ").title()} vs {y_label.replace("_", " ").title()}')
        plt.xticks(rotation=90)  # Rotate x-axis labels to avoid overlap
        
        # Save plot to a BytesIO object
        img = io.BytesIO()
        plt.savefig(img, format='png')
        img.seek(0)
        
        # Encode the image to base64
        graph_url = base64.b64encode(img.getvalue()).decode()
        
        return graph_url
    except KeyError as e:
        print(f"KeyError: {e}")
        return ""


    
@app.route('/logout', methods=['POST'])
def logout():
    session.pop('logged_in', None)
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))


if __name__ == '__main__':
    app.run(debug=True)
