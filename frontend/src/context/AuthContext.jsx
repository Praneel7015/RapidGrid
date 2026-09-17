import React, { createContext, useContext, useState } from 'react';

const AuthContext = createContext();

export function AuthProvider({ children }) {
  const [role, setRole] = useState(sessionStorage.getItem('geoagentic_role') || null);

  const [hospitalInfo, setHospitalInfo] = useState(() => {
    try {
      return JSON.parse(sessionStorage.getItem('geoagentic_hosp')) || { id: 'all', name: 'Bangalore Central ER Desk' };
    } catch {
      return { id: 'all', name: 'Bangalore Central ER Desk' };
    }
  });

  const [citizenInfo, setCitizenInfo] = useState(() => {
    try {
      return JSON.parse(sessionStorage.getItem('geoagentic_citizen')) || { name: 'Ananya Sharma', phone: '+91 98765 43210' };
    } catch {
      return { name: 'Ananya Sharma', phone: '+91 98765 43210' };
    }
  });

  const [driverInfo, setDriverInfo] = useState(() => {
    try {
      return JSON.parse(sessionStorage.getItem('geoagentic_driver')) || null;
    } catch {
      return null;
    }
  });

  const login = (selectedRole, extraData = null) => {
    setRole(selectedRole);
    sessionStorage.setItem('geoagentic_role', selectedRole);
    if (selectedRole === 'hospital' && extraData) {
      setHospitalInfo(extraData);
      sessionStorage.setItem('geoagentic_hosp', JSON.stringify(extraData));
    } else if (selectedRole === 'citizen' && extraData) {
      setCitizenInfo(extraData);
      sessionStorage.setItem('geoagentic_citizen', JSON.stringify(extraData));
    } else if (selectedRole === 'driver' && extraData) {
      setDriverInfo(extraData);
      sessionStorage.setItem('geoagentic_driver', JSON.stringify(extraData));
    }
  };

  const logout = () => {
    setRole(null);
    setDriverInfo(null);
    sessionStorage.removeItem('geoagentic_role');
    sessionStorage.removeItem('geoagentic_driver');
    sessionStorage.removeItem('geoagentic_hosp');
    sessionStorage.removeItem('geoagentic_citizen');
  };

  return (
    <AuthContext.Provider value={{ role, hospitalInfo, citizenInfo, driverInfo, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
