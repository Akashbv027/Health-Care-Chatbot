
// Store doctor objects by ID for safe reference
let doctorRegistry = {};
let doctorIdCounter = 0;

// Modified fetchDoctors to use safe doctor references
function fetchDoctors_new(lat,lng){ 
  doctorRegistry = {}; doctorIdCounter = 0;
  fetch('/doctors_nearby',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lat:lat,lng:lng,radius:5000})})
    .then(r=>r.json())
    .then(data=>{ 
      const list=document.getElementById('doctors-list'); 
      list.innerHTML=''; 
      (data.doctors||[]).slice(0,10).forEach(d=>{ 
        const docId = 'doc_' + (doctorIdCounter++);
        doctorRegistry[docId] = d;  // Store the doctor object safely
        const el=document.createElement('div'); 
        el.className='mb-2'; 
        el.innerHTML=`<strong>${d.name}</strong> <small class='text-muted'>${d.specialization}</small><br>${d.facility||''}<br><small>${d.availability||''}</small><br><a href='#' onclick='bookDoctor_new("${docId}");return false;' style='color:#0d6efd;text-decoration:underline;cursor:pointer;'>Book / Contact</a>`; 
        list.appendChild(el); 
      }); 
    })
    .catch(()=>{ document.getElementById('doctors-list').innerText='Failed to load doctors'; }); 
}

// Updated bookDoctor to use the registry
function bookDoctor_new(docId){ 
  try {
    currentBookingDoctor = doctorRegistry[docId]; 
    if (!currentBookingDoctor) { alert('Doctor not found'); return; }
    // Populate modal fields
    document.getElementById('booking-doctor-name').innerText = currentBookingDoctor.name || 'N/A';
    document.getElementById('booking-doctor-spec').innerText = currentBookingDoctor.specialization || 'N/A';
    document.getElementById('booking-doctor-facility').innerText = currentBookingDoctor.facility || 'N/A';
    document.getElementById('booking-doctor-availability').innerText = currentBookingDoctor.availability || 'N/A';
    document.getElementById('booking-doctor-phone').innerText = currentBookingDoctor.phone || 'Contact clinic for details';
    // Clear form fields
    document.getElementById('booking-date').value = '';
    document.getElementById('booking-time').value = '';
    document.getElementById('booking-concern').value = '';
    document.getElementById('booking-message').classList.add('d-none');
    // Show modal
    const modal = new bootstrap.Modal(document.getElementById('doctorBookingModal'));
    modal.show();
  } catch(e) { 
    alert('Error loading doctor details: ' + e.message); 
  }
}
